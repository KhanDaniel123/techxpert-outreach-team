# Test report - TechXpert Outreach v2 (Google-free)

Run: `python3 tests/test_all.py` (from the project root)
Date: 2026-09-28. DB: throwaway SQLite. Network: mocked (no real Gmail/IMAP calls).

**230 passed, 0 failed.**

## Auth (10)
Register success, duplicate email rejected, bad email rejected, short password
rejected, password stored as scrypt hash (never plaintext), login success,
wrong password rejected, unknown email rejected, logout + protected redirect,
accounts page requires login.

## Sender accounts (6)
Add route works with mocked credential verify; App Password stored
Fernet-encrypted and decrypts to the cleaned 16 chars; accounts page never
renders the secret; short App Password rejected (400); failed verification
rejected (400).

## SMTP sending (7)
Live send through mocked `smtplib.SMTP`: STARTTLS used, login with the
decrypted App Password, correct From/To/Subject/body; send_log status=sent,
step=0; daily counter incremented; `verify_credentials` reports auth failure
and success correctly.

## Follow-up configuration (13)
Campaign creation seeds 5 default follow-ups; campaign row defaults
(enabled=1, count=5, 40h); count=3 keeps exactly 3 steps; interval saved;
count clamps to 1..10; old-style template saves don't wipe follow-ups; blank
body deletes a step; form shows exactly N fields (steps beyond N hidden);
enable checkbox and 1-10 count input present.

## Follow-up chaining & gating (13)
Step-0 send queues step-1 ~40h out; future follow-up not sent early
(`waiting_for_followups`); step-1 sends when due with variables/spintax
rendered from its own template; send_log records steps; reply stops the
sequence (`sequence_stopped`, queue row failed); bounce stops it; missing
previous step blocks it; disabled follow-ups don't chain; chain stops at the
configured count; campaign page shows Replied / F1-queued per-lead status;
logs page shows Initial/F1 step pills.

## Reply & bounce detection (6)
`scan_replies` (mocked IMAP) returns dicts `{address, snippet, date}` for each
replier, skipping mailer-daemon and the account's own address, newest message
wins per address; `mark_replies` accepts the rich data (plain strings still
work), is case-insensitive and user-scoped, returns only newly-marked leads
with full context (business, email, campaign id/name, snippet, date), and
returns `[]` on repeat scans; `scan_bounces` extracts the failed recipient;
IMAP failures degrade to `[]` (never raise).

## Reply notifications + handled pipeline (15)
`notify_replies` inserts one `notifications` row per newly-replied lead
(deduped: no duplicates on repeat scans) and emails the user's login address
from their sender account with subject `Reply from {business} ({campaign})`,
the reply snippet, and a campaign link; with no sender account the
notification is still stored and the email is skipped gracefully. Dashboard
shows a "Replied leads" hot list (business, email, campaign, snippet, time)
with Open-campaign and Mark-handled actions; `handled=1` hides a lead from
the list; header bell shows the unread count and opening `/notifications`
marks everything read; the cron tick notifies through the same path without
duplicates.

## Preserved engine (20) + personalized_line (11)
Spintax (incl. nested), template variables, round-robin rotation, caps/paused
exclusion, 21-day warmup ramp, enqueue, dry-run processing + logging, dry-run
follow-up previews, campaign stays `sending` while follow-ups pending,
sending-window deferral, cron 401 without secret / 200 with secret (with
`replies_marked` in the response), cross-user campaign 404, user-scoped logs,
template save + preview, validator fast paths (bad syntax, disposable), mocked
validate and enrich job chunks, no `gmail_oauth`/`GOOGLE_CLIENT_ID`/
`GOOGLE_CLIENT_SECRET` references anywhere in app code.
`{personalized_line}` renders in subject/body, blank when empty or missing,
existing variables unaffected, unknown `{vars}` still left literally, spintax
combines with it, idempotent migration adds `leads.personalized_line`, CSV
import reads the column, manual add stores it, DB-row rendering works.

## Not covered by automated tests (do live after deploy)
One supervised real Gmail send (App Password login from Vercel's network),
one live IMAP reply/bounce scan, the external 10-minute cron actually
triggering `/api/process-queue`, and one real OpenAI generation (key set)
to confirm grounding quality and cost per lead.

## Autopilot AI SDR mode (38)
Gap analyzer: no-website / unreachable / full-site / bare-site findings are
pure observations; prompt contains the grounding rules (only observed facts,
never invent metrics, 120/60 word caps, 3 different follow-up angles);
findings + business context passed to the model verbatim; AI content
generated once and cached (second call makes zero API calls); skip when no
website and no business info (falls back to template, no AI call);
`_templates_for` uses AI email / AI follow-ups / cycles fu3 with a
"circling back" prefix beyond step 3; until-reply mode chains steps 1-4 and
never exceeds max_touches (5 sends, no step 5); reply stops the until-reply
sequence; autopilot without AI content falls back to the normal template;
settings saved and clamped via the template route (max_touches 1-20, bogus
mode resets to fixed); campaign page shows the toggle, per-lead cost, and
"AI written" status; settings page shows Enabled/Disabled; autopilot job
runs chunked (init, one lead, done); without a key the toggle is forced off,
the campaign page explains AI is off, the autopilot route refuses, and
normal templates still send. Avg cost per lead computed from stored tokens.

## AI provider selection: Gemini or OpenAI (13)
No key: provider is None and AI is off. Only OPENAI_API_KEY: provider is
openai and the OpenAI cost math is unchanged. Only GEMINI_API_KEY: provider
is gemini, cost is reported as 0.0 (free tier, nothing fabricated), and the
model name is the Gemini model. Both keys: Gemini is preferred. Call info
points at the Gemini OpenAI-compatible endpoint with the Gemini key and
model. A mocked request confirms the POST goes to
generativelanguage.googleapis.com/v1beta/openai/, uses the Gemini bearer key
and model, and the JSON response parses. Default model is gemini-2.0-flash,
overridable with GEMINI_MODEL.

## Full Autopilot pipeline (30)
Migration: `pipeline_enabled`, `pipeline_target_leads`, `pipeline_stage`,
`pipeline_cursor` exist on campaigns and `init_db()` is idempotent. Routes:
start refuses without niche+location (explains itself); save stores
niche/location/target (clamped 5-500); start enables and resets to discover;
pause disables; campaign page shows the pipeline box with live stats.
Discover: one tick inserts deduped leads from mocked web search, never
duplicates across ticks, stays in discover while the query rotation still
has angles to try, and advances to enrich only after a full rotation with
zero new leads. Enrich:
public email stored with pending verdict; no-email leads marked `none` and
never retried; completion advances to validate. Validate: good emails kept
as valid; invalid emails are dropped (address cleared so they can never
send); completion advances to write. Write: skipped without an API key
(normal templates used instead); with a key, AI runs only for validated
leads; completion advances to queue. Queue: only valid/risky, selected,
never-contacted leads are queued; unknown/invalid/no-email leads, replied
leads, and bounced leads are excluded; queueing is idempotent and completes
to a terminal done stage that never restarts. tick_all skips paused
campaigns and advances one stage per enabled campaign per tick.
`process_all` runs the pipeline tick inside the scheduler alongside the
reply scan, job chunk, and sends.

## Discovery filter (26)
Shared filter in `pipeline.py`, reused by the manual web-search job in
`jobs.py`. `discovery_verdict(name, url, niche, location)` returns
(keep, reason); every drop is written to the server logs via
`log_skipped` and the manual job reports its junk-skip count in its
result line. `AGGREGATOR_DOMAINS` (easy to extend) drops aggregator /
directory / social / search domains by domain part, so tripadvisor.de,
m.yelp.com etc. are caught too; the `x.com` entry cannot false-positive
on domains like box.com. `LISTICLE_PATTERNS` drops ranking/guide titles
("11 Top Locations for Your Training", "Best Gyms in Berlin (2026)",
"Top 10 Gyms in Berlin"). Placeholder names are dropped: "<city><number>"
("Berlin10") and names made only of niche/location words ("Berlin Gyms
and Fitness"). Real businesses (McFIT Berlin Mitte, FitX Kreuzberg, John
Reed Fitness) pass; empty names still fall back to the domain; empty URLs
are dropped.

## Discovery query diversification (14)
`build_discovery_queries(niche, location)` builds a rotating query list per
campaign: 3 English base variants, then German-language variants when the
location is in Germany (`NICHE_TRANSLATIONS["de"]` maps gym/fitness center to
Fitnessstudio/Fitnesscenter, falling back to the English niche), then one
query per district for mapped cities (`LOCATION_DISTRICTS`, keyed by
normalized city name, currently Berlin with 12 districts; unknown cities fall
back to the plain location query). `pick_query` rotates one query per tick
and wraps; the index is persisted in the campaign's `pipeline_cursor` ("q")
so it survives restarts (verified across real ticks). `DISCOVER_PER_TICK`
raised from 5 to 8. Each tick logs the query variant used. The existing
`discovery_verdict` junk filter is unchanged and still applied to every
result.

## Unsubscribe + compliance footer (29)
New `unsubscribe.py`: `signed_token(lead_id, user_id)` mints an opaque
Fernet-encrypted token (same key as the stored Gmail App Passwords via
`crypto.py`); `verify_token` rejects forged/tampered tokens, expired tokens
(2-year max age), tokens for unknown leads, and tokens whose lead belongs to
a different user. Tokens are non-deterministic (random IV), so leads cannot
be enumerated. Public route `GET /unsubscribe?t=<token>` (no login) marks
the lead unsubscribed and renders a confirmation page; invalid/expired tokens
get a safe error page (400) and flip nothing. `leads.unsubscribed` column
added (Table + idempotent migration in `db.py`). Guards, same style as the
reply/bounce guards: `enqueue_campaign` excludes unsubscribed leads,
`pipeline._queue` skips them, `sender.sequence_gate` stops follow-ups, and
`queue_worker._process_campaign` drops queue items for leads that
unsubscribed after being queued (marked failed, never mailed, nothing in
send_log). Every outgoing campaign email and follow-up gets a plain-text
footer in `sender.send_one` (post-render, so it covers AI and template
paths): company name + physical address + one-click unsubscribe link.
`smtp_mail.build_message`/`send_message` accept `list_unsub_url` and set
`List-Unsubscribe: <url>` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click`
(RFC 2369/8058). Company name/address are per-user Settings
(`user_settings` table, `db.get_user_settings`/`save_user_settings`,
`GET/POST /settings`, `templates/settings.html`); env vars
`COMPANY_NAME`/`COMPANY_ADDRESS` act as defaults. Defaults: "TechXpert" and
the obvious placeholder "REPLACE WITH YOUR BUSINESS ADDRESS"; the Settings
page shows a warning pill until a real address is saved.
