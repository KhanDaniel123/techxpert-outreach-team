# Test report - TechXpert Outreach v2 (Google-free)

Run: `/tmp/v2venv/bin/python tests/test_all.py`
Date: 2026-09-27. DB: throwaway SQLite. Network: mocked (no real Gmail/IMAP calls).

**83 passed, 0 failed.**

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

## Reply & bounce detection (4)
`scan_replies` (mocked IMAP) returns the replier's address, skips
mailer-daemon and the account's own address; `mark_replies` is
case-insensitive and user-scoped; `scan_bounces` extracts the failed
recipient; IMAP failures degrade to `[]` (never raise).

## Preserved engine (20)
Spintax (incl. nested), template variables, round-robin rotation, caps/paused
exclusion, 21-day warmup ramp, enqueue, dry-run processing + logging, dry-run
follow-up previews, campaign stays `sending` while follow-ups pending,
sending-window deferral, cron 401 without secret / 200 with secret (with
`replies_marked` in the response), cross-user campaign 404, user-scoped logs,
template save + preview, validator fast paths (bad syntax, disposable), mocked
validate and enrich job chunks, no `gmail_oauth`/`GOOGLE_CLIENT_ID`/
`GOOGLE_CLIENT_SECRET` references anywhere in app code.

## Not covered by automated tests (do live after deploy)
One supervised real Gmail send (App Password login from Vercel's network),
one live IMAP reply/bounce scan, and the external 10-minute cron actually
triggering `/api/process-queue`.
