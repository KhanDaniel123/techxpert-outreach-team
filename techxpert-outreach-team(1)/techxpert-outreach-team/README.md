# TechXpert Outreach (team version, v2)

Lead discovery, website enrichment, email validation, and Gmail outreach with
multi-account rotation, per-account daily caps, warmup ramp, spintax,
randomized delays, and sending windows.

**How it works:** every team member creates their own login (email +
password) and only ever sees their own sender accounts, campaigns, leads,
and send logs. Each person connects their own Gmail using a Google **App
Password** - no Google Cloud project, no OAuth, no admin work per person.
Sending is not live in a background thread: an external scheduler pings the
app every 10 minutes and the app processes its queue (a Vercel cron entry
exists only as a once-a-day safety net).

## Deploy it (non-technical steps, in order)

1. **Push this folder to GitHub** (as a new repository), then in Vercel click
   "Add New Project" and import it. Do not change any code.

2. **Create the database:** sign up at [neon.tech](https://neon.tech) (free
   tier), create a project, and copy the connection string. It looks like
   `postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/dbname?sslmode=require`.

   If you are upgrading from the Google-OAuth version of this app, point v2
   at the **same** Neon database: v2 creates its own tables (`app_users`,
   `sender_accounts`) automatically on first boot and ignores the old ones.
   Nothing is deleted.

3. **Generate the encryption key** (run this once on any computer with Python):
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Save the output. Saved Gmail App Passwords are encrypted with this key,
   and it also keeps login sessions stable, so **never change it later** or
   saved sender accounts stop working and everyone gets logged out.

4. **Set environment variables in Vercel** (Project Settings -> Environment
   Variables). Exactly these four:
   - `APP_URL` (step 5 below - set after first deploy)
   - `DATABASE_URL` (the Neon string from step 2)
   - `FERNET_KEY` (the key from step 3)
   - `CRON_SECRET` (any long random string you invent, e.g. 32 random
     characters; the scheduler uses it to prove it is allowed to wake the app)

   There is no `GOOGLE_CLIENT_ID`, no `GOOGLE_CLIENT_SECRET`, no `SECRET_KEY`
   (the session key is derived from `FERNET_KEY` automatically).

5. **Deploy once** in Vercel to get your public URL, something like
   `https://techxpert-outreach.vercel.app`. Then set `APP_URL` to exactly
   that URL (no trailing slash) in the environment variables and redeploy
   (Deployments -> Redeploy).

6. **Set up the scheduler** (this is what actually sends emails):
   - Option A (recommended, free): [cron-job.org](https://cron-job.org) ->
     create a job: URL `https://<your-app>.vercel.app/api/process-queue`,
     every 10 minutes, POST, with header
     `Authorization: Bearer <your CRON_SECRET>`.
   - Option B: this repo includes `.github/workflows/cron.yml`. Fork it to
     GitHub, add repository secrets `APP_URL` and `CRON_SECRET`, and GitHub
     will ping the app every 10 minutes on weekdays.
   - The built-in Vercel cron (once daily) is only a backup.

7. **First run:** register an account, connect your Gmail (see below),
   create a campaign, import the sample leads, keep **dry-run ON**, queue
   them, and watch the send log. Then add your own address as a lead, turn
   dry-run OFF for one supervised send, and check it arrived.

## How each teammate connects Gmail (self-service, ~2 minutes)

No admin needed. Each teammate does this once, on their own:

1. Open the app URL and **create an account** (Register page).
2. Go to **Sender accounts**.
3. In a new tab, open [myaccount.google.com](https://myaccount.google.com)
   -> **Security** -> turn on **2-Step Verification** (required by Google
   for the next step).
4. Still under **Security**, open **App passwords** (you may need to search
   for it), create one named `TechXpert Outreach`, and copy the
   16-character code it shows.
5. Paste the code into the app's **App Password** field and hit Connect.
   The app verifies it against Gmail immediately, so a typo is caught right
   there instead of at send time.

Notes:
- The App Password is stored encrypted and is never shown again. If it
  stops working (e.g. they change their Google password), they just create
  a new one and re-connect; the old entry is replaced.
- Regular Gmail accounts can send ~500 emails/day through this method.
- Teammates never see each other's accounts, campaigns, leads, or logs.

## Using it day to day

1. **Campaigns:** name, niche, location, then leads via CSV import, sample
   data, web search, or manual entry. Tick the leads you want to contact.
2. **Enrich:** crawls each selected lead's website for a public email address
   and contact form. Sites that show nothing are skipped after one attempt.
3. **Validate:** 4-stage email check (syntax, disposable domains, MX records,
   SMTP probe with catch-all detection). Verdicts: valid / invalid / risky /
   unknown.
4. **Template:** subject/body with `{variables}` and `{spintax|options}`.
   Preview renders a real lead through it.
5. **Queue & start:** the scheduler processes up to 5 per tick per campaign,
   random delays between messages, only inside the sending window, rotating
   sender accounts, never exceeding each account's daily cap (with a 21-day
   warmup ramp for new accounts).
6. **Sender accounts:** per-account daily cap, warmup on/off, pause/resume,
   and a bounce scanner (IMAP) that auto-pauses an account if bounce rate
   tops 10%.
7. **Send log:** every attempt, per campaign, account, recipient, and step
   (Initial / F1..F10).

## Follow-up sequences

Each campaign can run a follow-up sequence (campaign page, "Follow-up
sequence" section):

- **Enable/disable** checkbox, **1-10 steps** (default 5), and **hours between
  steps** (default 40, per campaign).
- The form shows exactly the configured number of follow-up subject/body
  fields. Each step has its own template with the same `{variables}` and
  `{a|b}` spintax as the main template. Five sensible defaults are seeded at
  campaign creation, so sequences work out of the box; blanking a step's body
  ends the sequence there.
- Rules: follow-up N only sends if step N-1 **sent successfully** AND **no
  reply was detected** AND **the address did not bounce**. A reply seen by the
  IMAP scan marks the lead replied and stops their sequence; the scheduler
  also runs the reply scan on every tick.
- The campaign page shows each lead's sequence state (Queued, Initial sent,
  F1 sent/F2 queued, Replied, Bounced); the send log shows the step of every
  attempt.

## Autopilot: AI writes each email (optional)

Autopilot turns the app into an autonomous AI SDR for a campaign: it visits
each lead's website, notes one thing it can actually observe, writes a short
personal email about that observation, and keeps following up until the lead
replies.

**Turning it on.** Add `GEMINI_API_KEY` (free from Google AI Studio) or
`OPENAI_API_KEY` in Vercel under Settings >
Environment Variables and redeploy. If both are set, Gemini is used. Without
a key, the Autopilot toggle is
hidden and everything works exactly as before. The key is never displayed
anywhere in the app; Settings just shows "AI writing: Enabled/Disabled".

**How it works, per campaign:**

1. Tick **"Let AI write a personal email for each lead"** in the campaign's
   Autopilot section and save.
2. Click **Write AI emails**. A background job visits each selected lead's
   website and runs deterministic checks (reachable? contact email/phone
   listed? booking or quote option? reviews? social links? mobile layout?
   slow to load?). These are plain observed facts, never guesses.
3. A cheap AI model (`gpt-4o-mini`) writes one short email plus 3 follow-ups
   per lead, grounded ONLY in those observed facts. The prompt forbids
   inventing metrics, pain, revenue, or capabilities, and caps length
   (120 words for the opener, 60 per follow-up).
4. Each lead's email is generated once and cached, then reused. Leads with
   no website and no business info are skipped and fall back to your normal
   template (marked "Uses template" on the campaign page).
5. Queue and start sending as usual. Step 0 sends the AI email, steps 1-3
   send the AI follow-ups; beyond that the 3rd follow-up keeps cycling with
   a polite "circling back" variation.

**Follow-up mode.** Each campaign chooses: **Fixed sequence** (the default;
uses the follow-up steps below the template) or **Keep following up until
they reply**. In "until they reply" mode you set **max emails per lead**
(default 7, allowed 1-20). 7 is the default because it means roughly one
nudge every 40 hours for about 10 days: enough touches to catch busy people
without becoming spam. Most replies come in the first few touches, and the
sequence always stops the moment a lead replies or their email bounces,
exactly like the fixed mode.

**Cost.** About a fraction of a cent per lead (roughly $0.15 per million
input tokens and $0.60 per million output tokens at current pricing; check
openai.com/pricing as prices change). The campaign page shows your real
average ("about $0.00X per email, based on N written") once the first emails
are written, computed from actual token usage.

**Honest notes.** The AI only knows what is publicly visible on the
website; the observation is a starting hook, not a diagnosis. Reply
detection still runs on every scheduler tick, so a reply stops all future
touches and notifies you immediately. AI writing runs a little at a time on
the scheduler (one lead per tick) so it never times out on serverless.

## Local development

```bash
./run.sh
```

Uses SQLite (`./outreach.db`) unless `DATABASE_URL` is set. The "Process
queue now" button on the dashboard runs one scheduler tick without cron.
Tests: `python tests/test_all.py` (throwaway SQLite DB, mocked network).

## Honest limits

- Vercel Hobby has serverless execution-time and cron-frequency limits. The
  **external 10-minute cron is the real queue trigger**; the daily Vercel cron
  is only a safety net.
- Cold starts can delay the first request after idle.
- Sending windows run in **UTC** on Vercel unless timezone support is added.
- Outbound SMTP port 25 is often blocked on serverless platforms. When it is,
  email validation degrades gracefully to `unknown` instead of crashing.
  (Sending itself uses ports 587/993, which are fine.)
- Gmail, Yahoo, and Microsoft may reject or throttle mailbox probes.
- SQLite on Vercel is **not persistent**; production must use Neon/Postgres.
- Bounce scanning reads the sender's inbox over IMAP; if IMAP is unreachable
  the scan degrades to "no bounces found" instead of failing.
- No Facebook or Instagram automation in this tool.
