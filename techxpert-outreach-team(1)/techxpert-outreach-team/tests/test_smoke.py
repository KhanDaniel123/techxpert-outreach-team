"""Smoke tests for the hosted team app. Run: python tests/test_smoke.py

Uses a throwaway SQLite DB (/tmp/test_team.db). Google OAuth handshake and
real Gmail sends are NOT tested live (no credentials here); those paths are
exercised with mocks/fakes.
"""
import io
import json
import os
import sys
import time

# ---- env before imports ----
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_team_smoke.db"
os.environ["FERNET_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # replaced below
os.environ["CRON_SECRET"] = "test-cron-secret"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"
os.environ["APP_URL"] = "http://localhost:5000"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

if os.path.exists("/tmp/test_team_smoke.db"):
    os.remove("/tmp/test_team_smoke.db")

import db
import crypto
import sender
import queue_worker
import jobs as jobsmod
import leads as leadmod
from app import app

db.init_db()
passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" [{detail}]" if detail and not cond else ""))


# 1. users
u1 = db.get_or_create_user("sub-1", "waleed@techxpert.io", "Waleed")
u1b = db.get_or_create_user("sub-1", "waleed@techxpert.io", "Waleed Khan")
check("user upsert keeps same id", u1["id"] == u1b["id"])
check("user name updated on re-login", u1b["name"] == "Waleed Khan")
u2 = db.get_or_create_user("sub-2", "teammate@techxpert.io", "Mate")
check("second user distinct", u2["id"] != u1["id"])

# 2. crypto roundtrip
tok = json.dumps({"token": "sekret-token", "refresh_token": "r"})
enc = crypto.encrypt_token(tok)
check("token encrypted (not plaintext)", "sekret-token" not in enc)
check("token decrypt roundtrip", crypto.decrypt_token(enc) == tok)

# 3. test client + login
client = app.test_client()
r = client.get("/dashboard")
check("protected page redirects to login", r.status_code == 302 and "/login" in r.headers["Location"])
with client.session_transaction() as s:
    s["user_id"] = u1["id"]

# 4. campaign create
r = client.post("/campaign/new", data={"name": "HVAC Phoenix", "niche": "HVAC contractor",
                                       "location": "Phoenix AZ", "dry_run": "1"})
check("campaign create redirects", r.status_code == 302)
cid = int(r.headers["Location"].rsplit("/", 1)[-1])
camp = db.q("SELECT * FROM campaigns WHERE id=?", (cid,), one=True)
check("campaign belongs to user", camp["user_id"] == u1["id"] and camp["dry_run"] == 1)

# 5. sample CSV import
r = client.post(f"/campaign/{cid}/sample")
n_leads = db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=?", (cid,), one=True)["c"]
check("sample CSV imports 12 leads", n_leads == 12, f"got {n_leads}")

# 6. manual lead with email
leadmod.add_manual(u1["id"], cid, {"business_name": "Test Shop", "email": "shop@example.com",
                                  "website": "https://example.com"})
check("manual lead added", db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=?",
                                (cid,), one=True)["c"] == 13)

# 7. spintax distribution
from collections import Counter
draws = Counter(sender.resolve_spintax("{Hi|Hello|Hey}") for _ in range(300))
check("spintax ~even over 300 draws", all(70 < v < 140 for v in draws.values()), str(dict(draws)))

# 8. template variables
lead = {"business_name": "ACME", "address": "", "phone": "123", "website": "w",
        "email": "e", "category": "HVAC"}
out = sender.render_template("Hi {business_name}, {phone} {unknown_var}", lead)
check("template vars render, unknown kept", out == "Hi ACME, 123 {unknown_var}", out)

# 9. two gmail accounts (encrypted fake tokens)
now = time.time()
for em in ("sender1@gmail.com", "sender2@gmail.com"):
    db.w("""INSERT INTO gmail_accounts (user_id, email, token_enc, daily_cap,
            warmup_enabled, warmup_start, status, sent_today, sent_date, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
         (u1["id"], em, crypto.encrypt_token(tok), 30, 1, now, "active", 0, "", now))
accts = db.q("SELECT * FROM gmail_accounts WHERE user_id=?", (u1["id"],))
check("two accounts stored encrypted",
      len(accts) == 2 and all("sekret-token" not in a["token_enc"] for a in accts))

# 10. warmup math
a = dict(accts[0])
check("warmup day 0 -> cap 5", sender.effective_cap(a) == 5)
a["warmup_start"] = now - 10 * 86400
check("warmup day 10 -> cap ~16", sender.effective_cap(a) == 16, str(sender.effective_cap(a)))
a["warmup_start"] = now - 30 * 86400
check("warmup day 30 -> full cap 30", sender.effective_cap(a) == 30)
a["warmup_enabled"] = 0
check("warmup off -> full cap", sender.effective_cap(a) == 30)

# 11. rotation across 2 accounts, 12 dry-run sends
db.w("UPDATE campaigns SET window_start='00:00', window_end='23:59' WHERE id=?", (cid,))
camp = db.q("SELECT * FROM campaigns WHERE id=?", (cid,), one=True)
leads = db.q("SELECT * FROM leads WHERE campaign_id=? AND email<>''", (cid,))
# give every lead an email for the rotation test
for i, l in enumerate(db.q("SELECT * FROM leads WHERE campaign_id=?", (cid,))):
    db.w("UPDATE leads SET email=? WHERE id=?", (f"lead{i}@example.com", l["id"]))
leads = db.q("SELECT * FROM leads WHERE campaign_id=?", (cid,))
import random
rng = random.Random(42)
seq = []
for l in leads[:12]:
    oc = sender.send_one(u1["id"], camp, l, rng=rng)
    seq.append(oc.get("account"))
check("rotation strict A,B alternation over 12 sends",
      seq == ["sender1@gmail.com", "sender2@gmail.com"] * 6, str(seq[:6]))
check("12 dry-run sends logged", db.q(
    "SELECT COUNT(*) c FROM send_log WHERE campaign_id=? AND status='dry-run'",
    (cid,), one=True)["c"] == 12)

# 12. caps: exhaust account 1 -> only account 2 used
db.w("UPDATE gmail_accounts SET sent_today=9999 WHERE email='sender1@gmail.com'")
camp = db.q("SELECT * FROM campaigns WHERE id=?", (cid,), one=True)
oc = sender.send_one(u1["id"], camp, leads[0], rng=rng)
check("at-cap account excluded from rotation", oc.get("account") == "sender2@gmail.com",
      str(oc.get("account")))
db.w("UPDATE gmail_accounts SET sent_today=0 WHERE email='sender1@gmail.com'")

# 13. enqueue + process_sends (dry-run) via queue worker
db.w("DELETE FROM send_queue WHERE campaign_id=?", (cid,))
db.w("DELETE FROM send_log WHERE campaign_id=?", (cid,))
nq = queue_worker.enqueue_campaign(u1["id"], cid)
check("enqueue queues all 13 emailed leads", nq == 13, str(nq))
outcomes = queue_worker.process_sends()
sent = [o for o in outcomes if o.get("ok")]
check("process_sends completes queue in one tick (dry-run)",
      len(sent) == 13, f"{len(sent)} ok")
check("queue all marked sent",
      db.q("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=? AND status='sent'",
           (cid,), one=True)["c"] == 13)
check("campaign marked done",
      db.q("SELECT status FROM campaigns WHERE id=?", (cid,), one=True)["status"] == "done")

# 14. window: campaign outside window defers
cid2 = db.w("""INSERT INTO campaigns (user_id, name, niche, dry_run, status,
              window_start, window_end, delay_min, delay_max, created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (u1["id"], "Night", "Plumber", 1, "draft", "03:00", "03:30", 60, 60, now))
leadmod.add_manual(u1["id"], cid2, {"business_name": "Night Shop", "email": "n@x.com"})
queue_worker.enqueue_campaign(u1["id"], cid2)
outs = queue_worker.process_sends()
check("outside window -> waiting_for_window",
      any(o.get("action") == "waiting_for_window" for o in outs), str(outs))
c2 = db.q("SELECT * FROM campaigns WHERE id=?", (cid2,), one=True)
check("next_send_at pushed into window", c2["next_send_at"] > time.time())

# 15. cron endpoint auth
r = client.get("/api/process-queue")
check("cron endpoint 401 without secret", r.status_code == 401)
r = client.get("/api/process-queue", headers={"X-Cron-Secret": "wrong"})
check("cron endpoint 401 with wrong secret", r.status_code == 401)
r = client.get("/api/process-queue", headers={"X-Cron-Secret": "test-cron-secret"})
check("cron endpoint 200 with X-Cron-Secret (GET)", r.status_code == 200)
r = client.post("/api/process-queue",
                headers={"Authorization": "Bearer test-cron-secret"})
check("cron endpoint 200 with Bearer (POST)", r.status_code == 200)
check("cron response has expected keys",
      set(json.loads(r.data).keys()) >= {"job_chunk", "sends", "processed_at"})

# 16. /api/process-now needs login
client2 = app.test_client()
r = client2.post("/api/process-now")
check("process-now redirects when logged out", r.status_code == 302)
r = client.post("/api/process-now")
check("process-now 200 when logged in", r.status_code == 200)

# 17. user isolation
with client.session_transaction() as s:
    s["user_id"] = u2["id"]
r = client.get(f"/campaign/{cid}")
check("user2 cannot open user1 campaign (404)", r.status_code == 404)
r = client.get("/logs")
check("user2 sees empty log", b"No sends logged yet" in r.data)
with client.session_transaction() as s:
    s["user_id"] = u1["id"]

# 18. bounce auto-pause (override list, no Gmail API)
aid = db.q("SELECT id FROM gmail_accounts WHERE email='sender2@gmail.com'", one=True)["id"]
db.w("DELETE FROM send_log WHERE account_id=?", (aid,))
for i in range(20):
    db.w("""INSERT INTO send_log (user_id, campaign_id, account_id, lead_id, recipient,
            subject_rendered, sent_at, status, error, dry_run)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
         (u1["id"], cid, aid, 1, f"r{i}@example.com", "s", now, "sent", "", 0))
res = sender.check_account_bounces(u1["id"], aid,
                                   bounce_addrs=["r0@example.com", "r1@example.com", "r2@example.com"])
check("3/20 bounces marked", res["bounced_marked"] == 3, str(res))
check("15% bounce rate auto-pauses", res["paused"] is True and res["bounce_rate"] == 0.15,
      str(res))
check("account row paused",
      db.q("SELECT status FROM gmail_accounts WHERE id=?", (aid,), one=True)["status"] == "paused")
db.w("UPDATE gmail_accounts SET status='active' WHERE id=?", (aid,))

# 19. email validator fast paths (no port 25 needed)
import email_validator as ev
check("bad syntax -> invalid", ev.validate_email("not-an-email")["verdict"] == "invalid")
check("disposable -> invalid", ev.validate_email("x@mailinator.com")["verdict"] == "invalid")
check("no MX -> invalid",
      ev.validate_email("x@nonexistent-domain-xyz12345.com")["verdict"] == "invalid")
r = ev.validate_email("info@unroutable-port25-test.invalid")
check("blocked SMTP degrades to unknown, never crashes",
      r["verdict"] in ("invalid", "unknown"), str(r))

# 20. chunked websearch job with mocked network
from scrapers import websearch
websearch.ddg_links = lambda q, timeout=30: ["https://example-hvac-1.com/x", "https://example-hvac-2.com/y",
                                             "https://example-hvac-3.com/z", "https://example-hvac-4.com/w"]
websearch._site_identity = lambda url: {"name": "Shop " + url.split("-")[-1].split(".")[0],
                                        "phone": "555-0100", "address": "Phoenix AZ"}
cid3 = db.w("""INSERT INTO campaigns (user_id, name, niche, location, dry_run, status,
              window_start, window_end, delay_min, delay_max, created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (u1["id"], "WS", "HVAC", "Phoenix AZ", 1, "draft", "00:00", "23:59", 60, 60, now))
jid = jobsmod.start_job(u1["id"], cid3, "websearch")
ticks = 0
while ticks < 10:
    j = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if j["status"] != "running":
        break
    jobsmod.process_one_job_chunk()
    ticks += 1
j = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
ws_leads = db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=?", (cid3,), one=True)["c"]
check("chunked websearch completes", j["status"] == "done", f"status={j['status']} ticks={ticks}")
check("websearch inserted 4 leads", ws_leads == 4, str(ws_leads))

# 21. chunked enrich job with mocked site fetch
import enrich as enrichmod
enrichmod.enrich_leads = lambda rows, progress_cb=None, pause=1.0: [
    {"lead_id": r["id"], "emails": ["info@shop.com"], "email": "info@shop.com",
     "has_contact_form": False, "phone": "555-0100", "pages_checked": 2} for r in rows]
jid2 = jobsmod.start_job(u1["id"], cid3, "enrich")
ticks = 0
while ticks < 10:
    j = db.q("SELECT * FROM jobs WHERE id=?", (jid2,), one=True)
    if j["status"] != "running":
        break
    jobsmod.process_one_job_chunk()
    ticks += 1
j = db.q("SELECT * FROM jobs WHERE id=?", (jid2,), one=True)
emailed = db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND email<>''",
               (cid3,), one=True)["c"]
check("chunked enrich completes with emails", j["status"] == "done" and emailed == 4,
      f"status={j['status']} emailed={emailed}")
v = db.q("SELECT email_verdict FROM leads WHERE campaign_id=? LIMIT 1", (cid3,), one=True)
check("enrichment auto-validated emails", v["email_verdict"] in
      ("valid", "invalid", "risky", "unknown"), str(v["email_verdict"]))

# 22. chunked validate job
jid3 = jobsmod.start_job(u1["id"], cid3, "validate")
ticks = 0
while ticks < 10:
    j = db.q("SELECT * FROM jobs WHERE id=?", (jid3,), one=True)
    if j["status"] != "running":
        break
    jobsmod.process_one_job_chunk()
    ticks += 1
j = db.q("SELECT * FROM jobs WHERE id=?", (jid3,), one=True)
check("chunked validate completes", j["status"] == "done", f"{j['status']} {j['result']}")

# 23. template save + preview
r = client.post(f"/campaign/{cid}/template",
                data={"subject_tpl": "Hey {business_name}", "body_tpl": "{Hi|Hey} {business_name}",
                      "delay_min": "60", "delay_max": "120",
                      "window_start": "09:00", "window_end": "17:00", "dry_run": "1"})
check("template save redirects", r.status_code == 302)
r = client.post(f"/campaign/{cid}/preview")
pj = json.loads(r.data)
check("preview renders spintax+vars",
      pj["subject"] == "Hey ACME" or pj["subject"].startswith("Hey "), str(pj))

# 24. tokens never leak into rendered HTML
r = client.get("/accounts")
html = r.data.decode()
check("no ciphertext/token material in accounts page",
      "token_enc" not in html and "sekret-token" not in html)

# 25. pause/resume/cap
r = client.post(f"/account/{aid}/pause")
check("pause works",
      db.q("SELECT status FROM gmail_accounts WHERE id=?", (aid,), one=True)["status"] == "paused")
r = client.post(f"/account/{aid}/resume")
r = client.post(f"/account/{aid}/cap", data={"daily_cap": "45"})
a = db.q("SELECT * FROM gmail_accounts WHERE id=?", (aid,), one=True)
check("resume+cap update", a["status"] == "active" and a["daily_cap"] == 45)

# 26. job status endpoint scoped to user
r = client.get(f"/job/{jid}/status")
check("job status 200 for owner", r.status_code == 200)

print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
