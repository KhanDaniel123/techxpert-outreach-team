# TechXpert Outreach (team version)

Lead discovery, website enrichment, email validation, and Gmail outreach with
multi-account rotation, per-account daily caps, warmup ramp, spintax,
randomized delays, and sending windows.

**How it works:** every team member signs in with Google and only ever sees
their own Gmail connections, campaigns, leads, and send logs. Sending is not
live in a background thread: an external scheduler pings the app every
10 minutes and the app processes its queue (a Vercel cron entry exists only
as a once-a-day safety net).

## Deploy it (non-technical steps, in order)

1. **Push this folder to GitHub** (as a new repository), then in Vercel click
   "Add New Project" and import it. Do not change any code.

2. **Create the database:** sign up at [neon.tech](https://neon.tech) (free
   tier), create a project, and copy the connection string. It looks like
   `postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/dbname?sslmode=require`.

3. **Generate the encryption key** (run this once on any computer with Python):
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Save the output. Gmail tokens are encrypted with this key, so **never
   change it later** or saved Gmail connections stop working.

4. **Set environment variables in Vercel** (Project Settings -> Environment
   Variables). All of them:
   - `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (step 6 below)
   - `APP_URL` (step 5 below)
   - `DATABASE_URL` (the Neon string from step 2)
   - `FERNET_KEY` (the key from step 3)
   - `CRON_SECRET` (any long random string you invent, e.g. 32 random
     characters; the scheduler uses it to prove it is allowed to wake the app)
   - `SECRET_KEY` (another long random string, for login sessions)

5. **Deploy once** in Vercel to get your public URL, something like
   `https://techxpert-outreach.vercel.app`. Then set `APP_URL` to exactly
   that URL (no trailing slash) in the environment variables.

6. **Google Cloud (one OAuth client covers both sign-in and Gmail):**
   - Go to [console.cloud.google.com](https://console.cloud.google.com),
     create a project, enable the **Gmail API**.
   - Create OAuth credentials of type **Web application**.
   - Under "Authorized redirect URIs" add BOTH:
     - `https://<your-app>.vercel.app/login/callback`
     - `https://<your-app>.vercel.app/oauth/callback`
   - Copy the Client ID and Client Secret into Vercel env vars.
   - Note: Gmail sending scopes are "restricted". A personal Gmail account
     may require Google's app verification before other people can connect;
     a Google Workspace account in **Internal** mode skips that.

7. **Redeploy** in Vercel (Deployments -> Redeploy) so the new variables take
   effect.

8. **Set up the scheduler** (this is what actually sends emails):
   - Option A (recommended, free): [cron-job.org](https://cron-job.org) ->
     create a job: URL `https://<your-app>.vercel.app/api/process-queue`,
     every 10 minutes, POST, with header
     `Authorization: Bearer <your CRON_SECRET>`.
   - Option B: this repo includes `.github/workflows/cron.yml`. Fork it to
     GitHub, add repository secrets `APP_URL` and `CRON_SECRET`, and GitHub
     will ping the app every 10 minutes on weekdays.
   - The built-in Vercel cron (once daily) is only a backup.

9. **First run:** sign in with Google, connect a Gmail account, create a
   campaign, import the sample leads, keep **dry-run ON**, queue them, and
   watch the send log. Then connect your real Gmail, add your own address as
   a lead, turn dry-run OFF for one supervised send, and check it arrived.

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
   Gmail accounts, never exceeding each account's daily cap (with a 21-day
   warmup ramp for new accounts).
6. **Accounts:** per-account daily cap, warmup on/off, pause/resume, and a
   bounce scanner that auto-pauses an account if bounce rate tops 10%.
7. **Send log:** every attempt, per campaign, account, and recipient.

## Local development

```bash
./run.sh
```

Uses SQLite (`./outreach.db`) unless `DATABASE_URL` is set. The "Process
queue now" button on the dashboard runs one scheduler tick without cron.

## Honest limits

- Vercel Hobby has serverless execution-time and cron-frequency limits. The
  **external 10-minute cron is the real queue trigger**; the daily Vercel cron
  is only a safety net.
- Cold starts can delay the first request after idle.
- Sending windows run in **UTC** on Vercel unless timezone support is added.
- Outbound SMTP port 25 is often blocked on serverless platforms. When it is,
  email validation degrades gracefully to `unknown` instead of crashing.
- Gmail, Yahoo, and Microsoft may reject or throttle mailbox probes.
- Personal Gmail users may need Google app verification for others to connect
  (Workspace Internal mode avoids this).
- SQLite on Vercel is **not persistent**; production must use Neon/Postgres.
- No Facebook or Instagram automation in this tool.
