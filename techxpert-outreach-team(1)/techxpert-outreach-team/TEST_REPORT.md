# TEST_REPORT

Date: 2026-09-27. App: TechXpert Outreach (team / hosted), repo
`~/workspace/techxpert-outreach-team`. Suite: `tests/test_smoke.py`.

## Tested locally (SQLite, mocked external services): 53/53 PASS

- Mocked Google identity: user creation, repeat login keeps same id and
  refreshes name/email, two users are distinct.
- Token encryption round trip (Fernet): ciphertext stored, plaintext not
  present, decrypt returns original.
- Campaign creation via HTTP route, campaign scoped to the signed-in user.
- CSV sample import: 12 leads; manual lead add.
- Spintax: ~even distribution over 300 draws; template variables render,
  unknown variables left intact.
- Two Gmail accounts stored with encrypted tokens; warmup math (day 0 -> 5/day,
  day 10 -> ~16/day, day 30 -> full cap, warmup off -> full cap).
- Rotation: 12 dry-run sends alternate strictly A,B,A,B (found and fixed a
  real bug: the in-memory campaign dict went stale between back-to-back sends).
- Caps: at-cap account excluded from rotation.
- Enqueue + `process_sends` (dry-run): 13 leads queued, all processed in one
  tick, queue marked sent, campaign marked done.
- Sending windows: campaign outside its window defers with
  `waiting_for_window`, `next_send_at` pushed into the next window.
- Cron endpoint: 401 without secret, 401 with wrong secret, 200 with
  `X-Cron-Secret` (GET) and `Authorization: Bearer` (POST); response includes
  job_chunk/sends/processed_at.
- `/api/process-now`: redirects when logged out, 200 when logged in.
- Data isolation: user 2 gets 404 on user 1's campaign and sees an empty log.
- Bounce handling: 3/20 forced bounces marked, 15% rate auto-pauses account,
  account row set to paused.
- Email validator fast paths: bad syntax -> invalid, disposable domain ->
  invalid, no-MX domain -> invalid, blocked SMTP port 25 -> `unknown`
  (degrades, never crashes).
- Chunked serverless jobs with mocked network: web search (4 links -> 4 leads,
  completes over ticks), enrichment (emails stored, auto-validated), validation
  (completes).
- Template save + preview render via HTTP.
- Accounts page HTML contains no token ciphertext or `token_enc`.
- Account pause/resume/cap update via routes; job status endpoint 200 for owner.
- Boot test: `python app.py` serves the login page (200), `/dashboard`
  redirects to login when signed out, `/api/process-queue` returns 401
  without a secret.

Also verified: all pinned `requirements.txt` versions install cleanly
(Flask 3.1.0, SQLAlchemy 2.0.36, psycopg2-binary, google API clients,
cryptography, dnspython, bs4, requests).

Live-network spot check (not part of the suite): `enrich_leads` fetched
dayandnightair.com and extracted `info@dayandnightair.com` + a phone number;
parkerandsons.com blocked the fetch (bot protection); example.com has no
contact email. Matches the ~33% real-world hit rate seen in the local app.

## NOT tested live (requires real credentials / deployment)

- Vercel deployment and `api/index.py` serverless handler.
- Real Neon Postgres (`DATABASE_URL` Postgres path). The SQL is dialect-neutral
  (`?` -> named binds, `RETURNING id` on Postgres, `lastrowid` on SQLite), but
  it has not run against a real Postgres server.
- Real Google OAuth handshake (login + Gmail connect) and real Gmail sends.
- Real external cron delivery (cron-job.org / GitHub Actions schedule).
- Vercel's automatic `Authorization: Bearer $CRON_SECRET` cron header
  (code accepts it; not observed live).

## Bugs found and fixed during testing

1. SQLAlchemy 2.x rejects positional `?` tuples in `text()`: rewrote `?` to
   named binds (`:p0`...) and converted tuples to dicts; INSERT id retrieval
   now uses Postgres `RETURNING id` with SQLite `lastrowid` fallback.
2. NOT NULL columns without server defaults broke raw-SQL INSERTs: added
   `server_default` to all omittable flag columns; `next_send_at` made
   nullable (worker treats None as 0).
3. Rotation bug: `pick_account` updated the DB but the caller's in-memory
   campaign dict went stale, so back-to-back `send_one` calls reused one
   account. Now syncs the dict too.
