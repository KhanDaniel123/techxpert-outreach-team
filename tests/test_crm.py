"""CRM timeline tests: every touch on a lead is stored as an event.

Self-contained (own SQLite DB at /tmp/v2test_crm.db) so it can run
alongside tests/test_all.py without interference.

Run: ~/workspace/venvs/shakedown-venv/bin/python tests/test_crm.py
"""
import io
import os
import sys
import time
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DB_PATH = "/tmp/v2test_crm.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH
os.environ["FERNET_KEY"] = "6V9xJvJxQJ0QZk8mZQp3vQw9zY8xK7vJ6uQ5tR4sS3qP2oO1nN0mM9lL8kK7=="
os.environ["CRON_SECRET"] = "test-cron-secret-123"

from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

import db
import crm
import auth as authmod
import crypto as cryptomod
import sender
import unsubscribe as unsubmod
import queue_worker
import leads as leadmod
import app as appmod

db.init_db()

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def events_for(lead_id):
    return db.q("SELECT * FROM lead_events WHERE lead_id=? ORDER BY id", (lead_id,))


ok, user = authmod.register_user("crm@example.com", "Tester", "password123")
assert ok, user
UID = user["id"]

ACCT = db.w(
    """INSERT INTO sender_accounts (user_id, email, password_enc, daily_cap,
       warmup_enabled, warmup_start, status, sent_today, sent_date, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?)""",
    (UID, "sender@example.com", cryptomod.encrypt_token("abcdefghijklmnop"),
     500, 0, time.time(), "active", 0, "", time.time()))

CAMP_DRY = db.w(
    """INSERT INTO campaigns (user_id, name, niche, location, subject_tpl, body_tpl,
       delay_min, delay_max, window_start, window_end, dry_run, status, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (UID, "DryCamp", "plumbers", "Phoenix AZ", "Hi {business_name}",
     "Hello {business_name}", 60, 61, "00:00", "23:59", 1, "sending", time.time()))
CAMP_LIVE = db.w(
    """INSERT INTO campaigns (user_id, name, niche, location, subject_tpl, body_tpl,
       delay_min, delay_max, window_start, window_end, dry_run, status, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (UID, "LiveCamp", "plumbers", "Phoenix AZ", "Hi {business_name}",
     "Hello {business_name}", 60, 61, "00:00", "23:59", 0, "sending", time.time()))
db.w("INSERT INTO followups (campaign_id, step, subject_tpl, body_tpl, created_at)"
     " VALUES (?,?,?,?,?)", (CAMP_LIVE, 1, "Re: hi", "bump {business_name}", time.time()))
db.w("INSERT INTO followups (campaign_id, step, subject_tpl, body_tpl, created_at)"
     " VALUES (?,?,?,?,?)", (CAMP_DRY, 1, "Re: hi", "bump {business_name}", time.time()))

camp_dry = db.q("SELECT * FROM campaigns WHERE id=?", (CAMP_DRY,), one=True)
camp_live = db.q("SELECT * FROM campaigns WHERE id=?", (CAMP_LIVE,), one=True)


_mklead_n = [0]


def mklead(camp_id, email, name="Test Biz"):
    _mklead_n[0] += 1
    leadmod.add_manual(UID, camp_id, {"business_name": name, "email": email,
                                     "website": f"https://example-{_mklead_n[0]}.com"})
    return db.q("SELECT * FROM leads WHERE campaign_id=? AND email=?",
                (camp_id, email), one=True)


# 1. lead_created on manual add
l1 = mklead(CAMP_DRY, "one@example.com", "Biz One")
ev = events_for(l1["id"])
check("manual add logs lead_created",
      len(ev) == 1 and ev[0]["event_type"] == "lead_created"
      and "manually" in ev[0]["detail"])

# 2. lead_created on CSV import
n, merged, errors = leadmod.import_csv(
    UID, CAMP_DRY, io.BytesIO(b"business_name,email\nCSV Biz,csv@example.com\n"),
    source="csv")
lcsv = db.q("SELECT * FROM leads WHERE email=?", ("csv@example.com",), one=True)
ev = events_for(lcsv["id"])
check("csv import logs lead_created",
      n == 1 and len(ev) == 1 and ev[0]["event_type"] == "lead_created"
      and "csv" in ev[0]["detail"].lower())

# 3. dry-run send logs email_sent with dry-run spelled out
l2 = mklead(CAMP_DRY, "two@example.com", "Biz Two")
db.w("UPDATE leads SET email_verdict='valid' WHERE id=?", (l2["id"],))
l2 = db.q("SELECT * FROM leads WHERE id=?", (l2["id"],), one=True)
out = sender.send_one(UID, camp_dry, l2, rng=__import__("random").Random(1))
ev = [e for e in events_for(l2["id"]) if e["event_type"] == "email_sent"]
check("dry-run send ok", out.get("ok") and out.get("dry_run"))
check("dry-run send logs email_sent as dry-run",
      len(ev) == 1 and "dry-run" in ev[0]["detail"]
      and '"dry_run": true' in ev[0]["meta"])

# 4. live send logs email_sent as a real send
l3 = mklead(CAMP_LIVE, "three@example.com", "Biz Three")
db.w("UPDATE leads SET email_verdict='valid' WHERE id=?", (l3["id"],))
l3 = db.q("SELECT * FROM leads WHERE id=?", (l3["id"],), one=True)
sent = []
out = sender.send_one(UID, camp_live, l3, send_fn=lambda a, t, s, b: sent.append(t),
                      rng=__import__("random").Random(1))
ev = [e for e in events_for(l3["id"]) if e["event_type"] == "email_sent"]
check("live send ok", out.get("ok") and not out.get("dry_run") and sent == ["three@example.com"])
check("live send logs email_sent as real",
      len(ev) == 1 and "sent to three@example.com" in ev[0]["detail"]
      and "dry-run" not in ev[0]["detail"]
      and '"dry_run": false' in ev[0]["meta"])

# 5. follow-up step logs followup_sent
out = sender.send_one(UID, camp_live, l3, step=1,
                      send_fn=lambda a, t, s, b: None,
                      rng=__import__("random").Random(1))
ev = [e for e in events_for(l3["id"]) if e["event_type"] == "followup_sent"]
check("follow-up step=1 ok", out.get("ok"))
check("follow-up logs followup_sent",
      len(ev) == 1 and "Follow-up 1" in ev[0]["detail"]
      and '"step": 1' in ev[0]["meta"])

# 6. reply detection logs reply_detected; the queued drop is not double-logged
l4 = mklead(CAMP_DRY, "four@example.com", "Biz Four")
db.w("UPDATE leads SET email_verdict='valid' WHERE id=?", (l4["id"],))
db.w("INSERT INTO send_queue (campaign_id, lead_id, contact_id, status, scheduled_at, step)"
     " VALUES (?,?,NULL,'pending',?,1)", (CAMP_DRY, l4["id"], time.time()))
newly = sender.mark_replies(UID, [{"address": "four@example.com",
                                   "snippet": "sounds good, call me",
                                   "date": None}])
ev = [e for e in events_for(l4["id"]) if e["event_type"] == "reply_detected"]
check("reply marked", len(newly) == 1 and newly[0]["lead_id"] == l4["id"])
check("reply_detected event logged with snippet",
      len(ev) == 1 and "sequence stopped" in ev[0]["detail"]
      and "sounds good" in ev[0]["meta"])
camp_dry2 = db.q("SELECT * FROM campaigns WHERE id=?", (CAMP_DRY,), one=True)
outcomes = queue_worker._process_campaign(camp_dry2, time.time())
stops = [o for o in outcomes if o.get("action") == "sequence_stopped"]
check("replied lead dropped from queue", len(stops) == 1)
check("no duplicate sequence_stopped for the replied drop",
      not [e for e in events_for(l4["id"]) if e["event_type"] == "sequence_stopped"])

# 7. bounce scan logs bounced
l5 = mklead(CAMP_LIVE, "five@example.com", "Biz Five")
db.w("UPDATE leads SET email_verdict='valid' WHERE id=?", (l5["id"],))
l5 = db.q("SELECT * FROM leads WHERE id=?", (l5["id"],), one=True)
sender.record_send(UID, CAMP_LIVE, ACCT, l5["id"], "five@example.com",
                   "Sub", status="sent", dry_run=False, step=0)
res = sender.check_account_bounces(UID, ACCT, bounce_addrs=["five@example.com"])
ev = [e for e in events_for(l5["id"]) if e["event_type"] == "bounced"]
check("bounce marked", res["bounced_marked"] == 1)
check("bounced event logged",
      len(ev) == 1 and "five@example.com" in ev[0]["detail"])

# 8. unsubscribe logs unsubscribed
l6 = mklead(CAMP_DRY, "six@example.com", "Biz Six")
check("mark_unsubscribed true", unsubmod.mark_unsubscribed(l6["id"], UID))
ev = [e for e in events_for(l6["id"]) if e["event_type"] == "unsubscribed"]
check("unsubscribed event logged", len(ev) == 1 and "never mailed again" in ev[0]["detail"])

# 9. backfill: exactly one lead_created, dated at the lead's created_at
raw_id = db.w(
    "INSERT INTO leads (user_id, campaign_id, business_name, email, created_at)"
    " VALUES (?,?,?,?,?)", (UID, CAMP_DRY, "Raw Biz", "raw@example.com", 1700000000.0))
db.w("DELETE FROM lead_events WHERE lead_id=?", (raw_id,))
db._backfill_lead_created_events()
ev = events_for(raw_id)
check("backfill creates one lead_created",
      len(ev) == 1 and ev[0]["event_type"] == "lead_created"
      and abs(ev[0]["created_at"] - 1700000000.0) < 0.01)
db._backfill_lead_created_events()
check("backfill is idempotent", len(events_for(raw_id)) == 1)

# 10. invalid event type rejected; log_event never raises on bad input
try:
    crm.log_event(raw_id, "nope")
    check("invalid event type rejected", False)
except ValueError:
    check("invalid event type rejected", True)
try:
    crm.log_event(None, "lead_created", "x")
    check("log_event never raises", True)
except Exception as e:
    check("log_event never raises", False, str(e))

# 11. lead timeline page renders 200, newest first
c = appmod.app.test_client()
r = c.post("/login", data={"email": "crm@example.com", "password": "password123"})
assert r.status_code == 302, r.status_code
l7 = mklead(CAMP_DRY, "seven@example.com", "Biz Seven")
crm.log_event(l7["id"], "enriched", "Public email found: seven@example.com")
crm.log_event(l7["id"], "email_validated", "Email valid: seven@example.com")
r = c.get(f"/lead/{l7['id']}")
body = r.data.decode()
check("lead page 200", r.status_code == 200)
check("timeline shows events newest first",
      body.index("Email valid") < body.index("Public email found")
      and body.index("Public email found") < body.index("Lead added manually"))
check("timeline has Timeline heading", "Timeline" in body)

# 12. campaign page shows the recent-activity feed
r = c.get(f"/campaign/{CAMP_DRY}")
body = r.data.decode()
check("campaign page 200", r.status_code == 200)
check("campaign shows Recent activity", "Recent activity" in body
      and "Lead added manually" in body)

# 13. manual suppression logs a suppressed event that survives the delete
l8 = mklead(CAMP_DRY, "eight@example.com", "Biz Eight")
r = c.post(f"/campaign/{CAMP_DRY}/leads/delete", data={"lead_id": str(l8["id"])},
           follow_redirects=False)
check("delete redirects", r.status_code == 302)
check("lead row gone", db.q("SELECT id FROM leads WHERE id=?", (l8["id"],), one=True) is None)
ev = [e for e in events_for(l8["id"]) if e["event_type"] == "suppressed"]
check("suppressed event survives the delete",
      len(ev) == 1 and "Biz Eight" in ev[0]["detail"])
r = c.get(f"/campaign/{CAMP_DRY}")
check("suppressed event visible in campaign activity",
      "Lead suppressed (deleted) by the user: Biz Eight" in r.data.decode())

# 14. sequence gate denial logs sequence_stopped (no dedicated event exists)
l9 = mklead(CAMP_DRY, "nine@example.com", "Biz Nine")
db.w("UPDATE leads SET email_verdict='valid' WHERE id=?", (l9["id"],))
# step 1 queued but step 0 never sent -> gate denies
db.w("INSERT INTO send_queue (campaign_id, lead_id, contact_id, status, scheduled_at, step)"
     " VALUES (?,?,NULL,'pending',?,1)", (CAMP_DRY, l9["id"], time.time()))
camp_dry3 = db.q("SELECT * FROM campaigns WHERE id=?", (CAMP_DRY,), one=True)
queue_worker._process_campaign(camp_dry3, time.time())
ev = [e for e in events_for(l9["id"]) if e["event_type"] == "sequence_stopped"]
check("gate denial logs sequence_stopped",
      len(ev) == 1 and "Previous step (0)" in ev[0]["detail"])

# 15. enriched + decision_makers_found + dry_run_queued + email_validated via pipeline
import pipeline as pipemod
camp_pipe = db.w(
    """INSERT INTO campaigns (user_id, name, niche, location, subject_tpl, body_tpl,
       delay_min, delay_max, window_start, window_end, dry_run, status,
       pipeline_enabled, pipeline_stage, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (UID, "PipeCamp", "plumbers", "Phoenix AZ", "Hi", "Hello",
     60, 61, "00:00", "23:59", 1, "draft", 1, "discover", time.time()))
campp = db.q("SELECT * FROM campaigns WHERE id=?", (camp_pipe,), one=True)
with mock.patch("pipeline.discovery_verdict", return_value=(True, "")):
    ok_new = pipemod._try_insert_lead(campp, "Pipe Biz", "https://pipe.example",
                                      "Addr", "123", "plumbers", "", "osm")
lp = db.q("SELECT * FROM leads WHERE campaign_id=?", (camp_pipe,), one=True)
check("pipeline discovery logs lead_created",
      ok_new and any(e["event_type"] == "lead_created" for e in events_for(lp["id"])))
with mock.patch("enrich.enrich_leads",
                return_value=[{"lead_id": lp["id"], "email": "pipe@pipe.example",
                               "phone": "", "address": "", "has_contact_form": False,
                               "pages_checked": 3, "page_text": ""}]):
    pipemod._enrich(campp)
check("pipeline enrich logs enriched",
      any(e["event_type"] == "enriched" and "pipe@pipe.example" in e["detail"]
          for e in events_for(lp["id"])))
with mock.patch("email_validator.validate_emails",
                return_value=[{"verdict": "valid", "reason": "mx ok", "mx_host": "m.example"}]):
    pipemod._validate(campp)
check("pipeline validate logs email_validated",
      any(e["event_type"] == "email_validated" and "valid" in e["detail"]
          for e in events_for(lp["id"])))
with mock.patch("decision_makers.enrich_lead", return_value=("done", 2)):
    pipemod._people(campp)
check("pipeline people logs decision_makers_found",
      any(e["event_type"] == "decision_makers_found" and "2 verified" in e["detail"]
          for e in events_for(lp["id"])))
pipemod._queue(campp)
check("pipeline queue logs dry_run_queued",
      any(e["event_type"] == "dry_run_queued" for e in events_for(lp["id"])))

print(f"\n{len(passed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
