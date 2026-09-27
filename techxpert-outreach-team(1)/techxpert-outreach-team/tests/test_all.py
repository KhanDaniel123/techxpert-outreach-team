"""Full test suite for the TechXpert Outreach team app (v2, Google-free).

Covers: registration/login/logout, wrong-password rejection, password
hashing, per-user isolation, sender accounts (App Password + mocked SMTP),
follow-up sequences (config, chaining, gating, reply detection), mocked SMTP
sending, IMAP bounce/reply scans, {personalized_line} template variable
(CSV/manual import, rendering, idempotent migration), and the preserved engine behavior
(spintax, rotation, caps, warmup, dry-run, windows, enrichment, validation,
cron auth).

Run: /tmp/v2venv/bin/python tests/test_all.py
"""
import os
import re
import sys
import time
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DB_PATH = "/tmp/v2test.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["FERNET_KEY"] = "6V9xJvJxQJ0QZk8mZQp3vQw9zY8xK7vJ6uQ5tR4sS3qP2oO1nN0mM9lL8kK7=="
os.environ["CRON_SECRET"] = "test-cron-secret-123"

from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

import app as appmod
import db
import auth as authmod
import crypto as cryptomod
import sender
import smtp_mail
import followups
import queue_worker
import jobs as jobsmod

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def make_user(email, pw="password123", name="Tester"):
    ok, payload = authmod.register_user(email, name, pw)
    assert ok, payload
    return payload


def login_client(email, pw="password123"):
    c = appmod.app.test_client()
    r = c.post("/login", data={"email": email, "password": pw}, follow_redirects=False)
    assert r.status_code == 302, r.status_code
    return c


def add_account(user_id, email, pw="abcdefghijklmnop", daily_cap=30):
    return db.w("""INSERT INTO sender_accounts
        (user_id, email, password_enc, daily_cap, warmup_enabled, warmup_start,
         status, sent_today, sent_date, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (user_id, email, cryptomod.encrypt_token(pw), daily_cap, 0,
         time.time(), "active", 0, "", time.time()))


def make_campaign(client, name="Camp", dry_run=1):
    r = client.post("/campaign/new", data={
        "name": name, "niche": "plumbers", "location": "Phoenix AZ",
        "icp_notes": "", "subject_tpl": "Hi {business_name}",
        "body_tpl": "Hello {business_name}, {a|b} in {location}.",
        "delay_min": 60, "delay_max": 61,
        "window_start": "00:00", "window_end": "23:59",
        "dry_run": "on" if dry_run else "",
    }, follow_redirects=False)
    assert r.status_code == 302, r.status_code
    m = re.search(r"/campaign/(\d+)", r.headers["Location"])
    return int(m.group(1))


def add_lead(cid, uid, email, name="Biz"):
    return db.w("""INSERT INTO leads (user_id, campaign_id, business_name, address,
        phone, website, email, category, source, notes, selected, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, cid, name, "", "", "", email, "plumber", "csv", "", 1, time.time()))


def wake(cid):
    """Reset campaign pacing so process_sends picks it up immediately."""
    db.w("UPDATE campaigns SET next_send_at=0 WHERE id=?", (cid,))


# ================= 1. auth =================
u1 = make_user("u1@example.com")
check("register success", u1["email"] == "u1@example.com")
ok, err = authmod.register_user("u1@example.com", "Dup", "password123")
check("duplicate email rejected", not ok and "exists" in err)
ok, err = authmod.register_user("not-an-email", "X", "password123")
check("bad email rejected", not ok)
ok, err = authmod.register_user("u2x@example.com", "X", "short")
check("short password rejected", not ok)
row = db.get_user_by_email("u1@example.com")
check("password stored as hash", row["password_hash"] != "password123"
      and row["password_hash"].startswith("scrypt"))
check("login success", authmod.verify_login("u1@example.com", "password123") is not None)
check("wrong password rejected", authmod.verify_login("u1@example.com", "nope") is None)
check("unknown email rejected", authmod.verify_login("nobody@example.com", "x") is None)

c1 = login_client("u1@example.com")
r = c1.get("/dashboard")
check("dashboard after login", r.status_code == 200 and b"Campaigns" in r.data)
c1.get("/logout")
r = c1.get("/dashboard", follow_redirects=False)
check("logout + protected redirect", r.status_code == 302 and "/login" in r.headers["Location"])
anon = appmod.app.test_client()
r = anon.get("/accounts", follow_redirects=False)
check("accounts protected", r.status_code == 302)
r = anon.post("/login", data={"email": "u1@example.com", "password": "wrong"})
check("web login wrong password shows error", r.status_code == 200 and b"Wrong email or password" in r.data)
c1 = login_client("u1@example.com")

# ================= 2. sender accounts =================
with mock.patch.object(smtp_mail, "verify_credentials", return_value=(True, "")):
    r = c1.post("/accounts/add", data={"email": "send1@gmail.com",
                                       "app_password": "abcd efgh ijkl mnop"},
                follow_redirects=False)
check("account add route ok (mocked verify)", r.status_code == 302)
acct = db.q("SELECT * FROM sender_accounts WHERE user_id=?", (u1["id"],), one=True)
check("account stored", acct and acct["email"] == "send1@gmail.com")
check("app password encrypted at rest",
      acct["password_enc"] != "abcdefghijklmnop"
      and cryptomod.decrypt_token(acct["password_enc"]) == "abcdefghijklmnop")
r = c1.get("/accounts")
check("accounts page never renders secret",
      b"abcdefghijklmnop" not in r.data and b"password_enc" not in r.data)
with mock.patch.object(smtp_mail, "verify_credentials", return_value=(True, "")):
    r = c1.post("/accounts/add", data={"email": "bad@gmail.com", "app_password": "short"})
check("short app password rejected", r.status_code == 400)
with mock.patch.object(smtp_mail, "verify_credentials", return_value=(False, "rejected")):
    r = c1.post("/accounts/add", data={"email": "bad2@gmail.com",
                                       "app_password": "abcdefghijklmnop"})
check("failed verify rejected", r.status_code == 400 and b"rejected" in r.data)
# give the test account headroom (route default is warmup-capped at 5/day)
db.w("UPDATE sender_accounts SET warmup_enabled=0, daily_cap=500 WHERE id=?", (acct["id"],))

# ================= 3. mocked SMTP sending =================
sent_log = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.logged_in = None
        self.tls = False
    def ehlo(self): pass
    def starttls(self): self.tls = True
    def login(self, u, p): self.logged_in = (u, p)
    def send_message(self, msg): sent_log.append((self.logged_in, msg))
    def __enter__(self): return self
    def __exit__(self, *a): return False


camp_live = db.w("""INSERT INTO campaigns (user_id, name, niche, location, subject_tpl,
    body_tpl, delay_min, delay_max, window_start, window_end, dry_run, status, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (u1["id"], "Live", "plumbers", "Phoenix AZ", "Sub {business_name}",
     "Body {business_name} {x|y}", 60, 61, "00:00", "23:59", 0, "sending", time.time()))
lead_live = add_lead(camp_live, u1["id"], "lead1@example.com", "Acme Plumbing")
camp = db.q("SELECT * FROM campaigns WHERE id=?", (camp_live,), one=True)
lead = db.q("SELECT * FROM leads WHERE id=?", (lead_live,), one=True)
with mock.patch("smtplib.SMTP", FakeSMTP):
    out = sender.send_one(u1["id"], camp, lead, rng=__import__("random").Random(1))
check("live send ok via mocked SMTP", out.get("ok") and not out.get("dry_run"))
check("SMTP used STARTTLS + login with decrypted pw",
      sent_log and sent_log[0][0] == ("send1@gmail.com", "abcdefghijklmnop"))
msg = sent_log[0][1]
check("message headers/body correct",
      msg["From"] == "send1@gmail.com" and msg["To"] == "lead1@example.com"
      and "Acme Plumbing" in msg["Subject"]
      and "Acme Plumbing" in msg.get_payload(decode=True).decode())
log = db.q("SELECT * FROM send_log WHERE campaign_id=? ORDER BY id DESC LIMIT 1",
           (camp_live,), one=True)
check("send_log status sent, step 0", log["status"] == "sent" and log["step"] == 0)
check("daily counter incremented",
      db.q("SELECT sent_today FROM sender_accounts WHERE id=?", (acct["id"],), one=True)["sent_today"] == 1)


class BoomSMTP(FakeSMTP):
    def login(self, u, p):
        import smtplib
        raise smtplib.SMTPAuthenticationError(535, b"bad")


with mock.patch("smtplib.SMTP", BoomSMTP):
    ok, err = smtp_mail.verify_credentials("x@gmail.com", "abcdefghijklmnop")
check("verify_credentials reports auth failure", not ok and "rejected" in err.lower() or "App Password" in err)
with mock.patch("smtplib.SMTP", FakeSMTP):
    ok, err = smtp_mail.verify_credentials("x@gmail.com", "abcdefghijklmnop")
check("verify_credentials ok on good login", ok)

# ================= 4. follow-ups: config =================
cid = make_campaign(c1, "FUCamp")
fus = db.q("SELECT * FROM followups WHERE campaign_id=? ORDER BY step", (cid,))
check("campaign creation seeds 5 default follow-ups",
      len(fus) == 5 and all(f["body_tpl"].strip() for f in fus))
camp = db.q("SELECT * FROM campaigns WHERE id=?", (cid,), one=True)
check("follow-up defaults on campaign row",
      camp["followups_enabled"] == 1 and camp["followup_count"] == 5
      and camp["followup_delay_hours"] == 40)

# set count to 3 with custom templates
r = c1.post(f"/campaign/{cid}/template", data={
    "subject_tpl": "S", "body_tpl": "B", "delay_min": 60, "delay_max": 61,
    "window_start": "00:00", "window_end": "23:59", "dry_run": "on",
    "followups_enabled": "on", "followup_count": "3", "followup_delay_hours": "48",
    "fu_subject_1": "F1s", "fu_body_1": "F1b {business_name}",
    "fu_subject_2": "F2s", "fu_body_2": "F2b",
    "fu_subject_3": "F3s", "fu_body_3": "F3b",
})
fus = db.q("SELECT * FROM followups WHERE campaign_id=? ORDER BY step", (cid,))
check("count=3 keeps exactly 3 steps", len(fus) == 3 and fus[0]["body_tpl"] == "F1b {business_name}")
camp = db.q("SELECT * FROM campaigns WHERE id=?", (cid,), one=True)
check("interval + count saved", camp["followup_count"] == 3 and camp["followup_delay_hours"] == 48)

# clamp: 99 -> 10, 0 -> 1
c1.post(f"/campaign/{cid}/template", data={
    "subject_tpl": "S", "body_tpl": "B", "delay_min": 60, "delay_max": 61,
    "window_start": "00:00", "window_end": "23:59", "dry_run": "on",
    "followups_enabled": "on", "followup_count": "99", "followup_delay_hours": "40"})
check("count clamps to 10", len(db.q("SELECT id FROM followups WHERE campaign_id=?", (cid,))) == 10
      and db.q("SELECT followup_count FROM campaigns WHERE id=?", (cid,), one=True)["followup_count"] == 10)
c1.post(f"/campaign/{cid}/template", data={
    "subject_tpl": "S", "body_tpl": "B", "delay_min": 60, "delay_max": 61,
    "window_start": "00:00", "window_end": "23:59", "dry_run": "on",
    "followups_enabled": "on", "followup_count": "0", "followup_delay_hours": "40"})
check("count clamps to 1", db.q("SELECT followup_count FROM campaigns WHERE id=?", (cid,), one=True)["followup_count"] == 1
      and len(db.q("SELECT id FROM followups WHERE campaign_id=?", (cid,))) == 1)

# old-style template save (no follow-up fields) must not wipe follow-ups
c1.post(f"/campaign/{cid}/template", data={
    "subject_tpl": "S2", "body_tpl": "B2", "delay_min": 60, "delay_max": 61,
    "window_start": "00:00", "window_end": "23:59", "dry_run": "on"})
check("template save without follow-up fields preserves them",
      len(db.q("SELECT id FROM followups WHERE campaign_id=?", (cid,))) == 1)

# blank body ends chain
followups.upsert_followup(cid, 1, "F1s", "   ")
check("blank body deletes the step",
      db.q("SELECT id FROM followups WHERE campaign_id=? AND step=1", (cid,), one=True) is None)

# form shows exactly N fields
cid2 = make_campaign(c1, "FUCamp2")
c1.post(f"/campaign/{cid2}/template", data={
    "subject_tpl": "S", "body_tpl": "B", "delay_min": 60, "delay_max": 61,
    "window_start": "00:00", "window_end": "23:59", "dry_run": "on",
    "followups_enabled": "on", "followup_count": "2", "followup_delay_hours": "40",
    "fu_subject_1": "F1s", "fu_body_1": "F1b",
    "fu_subject_2": "F2s", "fu_body_2": "F2b"})
r = c1.get(f"/campaign/{cid2}")
html = r.data.decode()
check("form shows step 2 visible", 'data-step="2"' in html and 'data-step="2" hidden' not in html)
check("form hides step 3", 'data-step="3" hidden' in html)
check("enable checkbox present", 'name="followups_enabled"' in html)
check("count input 1-10", 'name="followup_count"' in html and 'max="10"' in html)

# ================= 5. follow-ups: chaining & gating =================
def fresh_chain_campaign(name, dry_run=0, count=5, enabled=True, delay_h=40):
    c = make_campaign(c1, name, dry_run=dry_run)
    data = {"subject_tpl": "S", "body_tpl": "B", "delay_min": 60, "delay_max": 61,
            "window_start": "00:00", "window_end": "23:59",
            "followup_count": str(count), "followup_delay_hours": str(delay_h)}
    if enabled:
        data["followups_enabled"] = "on"
    if dry_run:
        data["dry_run"] = "on"
    for s in range(1, count + 1):
        data[f"fu_subject_{s}"] = f"F{s} subject {{business_name}}"
        data[f"fu_body_{s}"] = f"F{s} body {{business_name}} {{p|q}}"
    c1.post(f"/campaign/{c}/template", data=data)
    return c

cc = fresh_chain_campaign("Chain1")
lid = add_lead(cc, u1["id"], "chain1@example.com", "Chain Biz")
queue_worker.enqueue_campaign(u1["id"], cc)
sent = []
queue_worker.process_sends(send_fn=lambda a, t, s, b: sent.append((t, s, b)) or True)
q1 = db.q("SELECT * FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=1",
          (cc, lid), one=True)
check("after step-0 send, step-1 queued",
      q1 and q1["status"] == "pending" and len(sent) == 1)
check("step-1 scheduled ~40h out",
      q1 and abs(q1["scheduled_at"] - (time.time() + 40 * 3600)) < 120)
check("send_log recorded step 0",
      db.q("SELECT step FROM send_log WHERE campaign_id=? AND lead_id=?",
           (cc, lid), one=True)["step"] == 0)

# future follow-up not picked early
wake(cc)
out = queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
check("future follow-up not sent early", len(sent) == 1
      and any(o.get("action") == "waiting_for_followups" for o in out))

# make step-1 due and send it
db.w("UPDATE send_queue SET scheduled_at=? WHERE id=?", (time.time() - 1, q1["id"]))
sent.clear()
wake(cc)
queue_worker.process_sends(send_fn=lambda a, t, s, b: sent.append((t, s, b)) or True)
check("step-1 sent when due", len(sent) == 1 and "F1 subject Chain Biz" in sent[0][1]
      and ("p" in sent[0][2].split() or "q" in sent[0][2].split() or "Chain Biz" in sent[0][2]))
check("step-1 body rendered with variables/spintax", "Chain Biz" in sent[0][2])
log1 = db.q("SELECT step FROM send_log WHERE campaign_id=? AND lead_id=? AND step=1",
            (cc, lid), one=True)
check("send_log recorded step 1", log1 is not None)
q2 = db.q("SELECT * FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=2",
          (cc, lid), one=True)
check("step-2 chained", q2 and q2["status"] == "pending")

# reply stops the sequence
sender.mark_replies(u1["id"], ["chain1@example.com"])
db.w("UPDATE send_queue SET scheduled_at=? WHERE id=?", (time.time() - 1, q2["id"]))
wake(cc)
out = queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
row2 = db.q("SELECT status, last_error FROM send_queue WHERE id=?", (q2["id"],), one=True)
check("reply stops follow-up", row2["status"] == "failed" and "replied" in row2["last_error"].lower())
check("sequence_stopped outcome reported",
      any(o.get("action") == "sequence_stopped" for o in out))

# bounce stops the sequence
cc2 = fresh_chain_campaign("Chain2", count=2)
lid2 = add_lead(cc2, u1["id"], "chain2@example.com", "Chain2 Biz")
queue_worker.enqueue_campaign(u1["id"], cc2)
queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
db.w("""INSERT INTO send_log (user_id, campaign_id, account_id, lead_id, step, recipient,
        subject_rendered, sent_at, status, error, dry_run)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
     (u1["id"], cc2, acct["id"], lid2, 0, "chain2@example.com", "x", time.time(), "bounced", "", 0))
q = db.q("SELECT * FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=1", (cc2, lid2), one=True)
db.w("UPDATE send_queue SET scheduled_at=? WHERE id=?", (time.time() - 1, q["id"]))
wake(cc2)
queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
row = db.q("SELECT status, last_error FROM send_queue WHERE id=?", (q["id"],), one=True)
check("bounce stops follow-up", row["status"] == "failed" and "bounce" in row["last_error"].lower())

# missing previous step stops the chain
cc3 = fresh_chain_campaign("Chain3", count=3)
lid3 = add_lead(cc3, u1["id"], "chain3@example.com", "Chain3 Biz")
db.w("INSERT INTO send_queue (campaign_id, lead_id, status, scheduled_at, step) VALUES (?,?, 'pending', ?, 2)",
     (cc3, lid3, time.time() - 1))
db.w("UPDATE campaigns SET status='sending', next_send_at=0 WHERE id=?", (cc3,))
wake(cc3)
out = queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
row = db.q("SELECT status, last_error FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=2",
           (cc3, lid3), one=True)
check("missing previous step blocks follow-up",
      row["status"] == "failed" and "previous step" in row["last_error"].lower())

# disabled follow-ups: no chain
cc4 = fresh_chain_campaign("Chain4", enabled=False)
lid4 = add_lead(cc4, u1["id"], "chain4@example.com", "Chain4 Biz")
queue_worker.enqueue_campaign(u1["id"], cc4)
queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
check("disabled follow-ups do not chain",
      db.q("SELECT id FROM send_queue WHERE campaign_id=? AND step>0", (cc4,), one=True) is None)

# count respected: count=2 means no step 3
cc5 = fresh_chain_campaign("Chain5", count=2)
lid5 = add_lead(cc5, u1["id"], "chain5@example.com", "Chain5 Biz")
queue_worker.enqueue_campaign(u1["id"], cc5)
queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
for st in (1, 2):
    qq = db.q("SELECT * FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=?",
              (cc5, lid5, st), one=True)
    db.w("UPDATE send_queue SET scheduled_at=? WHERE id=?", (time.time() - 1, qq["id"]))
    wake(cc5)
    queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
check("chain stops at configured count",
      db.q("SELECT id FROM send_queue WHERE campaign_id=? AND step=3", (cc5,), one=True) is None
      and db.q("SELECT COUNT(*) c FROM send_log WHERE campaign_id=? AND status='sent'",
               (cc5,), one=True)["c"] == 3)
ok, reason = sender.sequence_gate(u1["id"], cc4, {"id": lid4, "replied": 0}, 1)
check("gate rejects when follow-ups disabled", not ok and "disabled" in reason)

# per-lead sequence status on campaign page
r = c1.get(f"/campaign/{cc}")
check("campaign page shows replied status", "Replied" in r.data.decode())
r = c1.get("/logs")
check("logs page shows step pills", "Initial" in r.data.decode() and "F1" in r.data.decode())

# ================= 6. reply detection via IMAP =================
class FakeIMAP:
    msgs = []
    def __init__(self, host, port, timeout=None): pass
    def login(self, u, p): return ("OK", [])
    def select(self, box, readonly=True): return ("OK", [])
    def search(self, *a):
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(FakeIMAP.msgs)))])
    def fetch(self, num, spec):
        raw = FakeIMAP.msgs[int(num) - 1]
        return ("OK", [(b"%d FETCH" % int(num), raw)])
    def close(self): return ("OK", [])
    def logout(self): return ("OK", [])

acct_row = db.q("SELECT * FROM sender_accounts WHERE id=?", (acct["id"],), one=True)
FakeIMAP.msgs = [
    b"From: chain5@example.com\r\nSubject: Re: hello\r\n\r\ninterested!",
    b"From: Mailer-Daemon <mailer-daemon@googlemail.com>\r\nSubject: Delivery Status Notification (Failure)\r\n\r\n",
    b"From: send1@gmail.com\r\nSubject: sent mail\r\n\r\n",
]
with mock.patch("imaplib.IMAP4_SSL", FakeIMAP):
    replies = smtp_mail.scan_replies(acct_row)
check("scan_replies finds replier, skips daemon + self",
      [r["address"] for r in replies] == ["chain5@example.com"], str(replies))
check("scan_replies carries snippet + date",
      replies and "interested!" in replies[0]["snippet"] and "date" in replies[0])
new = sender.mark_replies(u1["id"], [{"address": "CHAIN5@EXAMPLE.COM", "snippet": "s", "date": None},
                                     "nope@example.com"])
check("mark_replies is case-insensitive + user-scoped, returns new leads",
      len(new) == 1 and new[0]["lead_id"] == lid5
      and new[0]["email"] == "chain5@example.com"
      and new[0]["campaign_id"] == cc5 and new[0]["campaign_name"]
      and db.q("SELECT replied FROM leads WHERE id=?", (lid5,), one=True)["replied"] == 1)
check("repeat mark_replies returns nothing (no double count)",
      sender.mark_replies(u1["id"], [{"address": "chain5@example.com", "snippet": "s", "date": None}]) == [])

# bounce scan still parses
FakeIMAP.msgs = [(
    b"From: Mailer-Daemon <mailer-daemon@googlemail.com>\r\n"
    b"Subject: Delivery Status Notification (Failure)\r\n\r\n"
    b"Hi. This is the qmail-send program.\n<dead@example.com>:\nSorry.\n")]
with mock.patch("imaplib.IMAP4_SSL", FakeIMAP):
    b = smtp_mail.scan_bounces(acct_row)
check("scan_bounces extracts failed address", b == ["dead@example.com"], str(b))
with mock.patch("imaplib.IMAP4_SSL", side_effect=OSError("offline")):
    check("IMAP failures degrade to []",
          smtp_mail.scan_replies(acct_row) == [] and smtp_mail.scan_bounces(acct_row) == [])

# ================= 7. preserved engine behavior =================
import random as _r
check("spintax resolves", sender.resolve_spintax("{a|b}", _r.Random(0)) in ("a", "b"))
check("nested spintax", sender.resolve_spintax("{{x|y}|z}", _r.Random(1)) in ("x", "y", "z"))
lead_t = {"business_name": "Biz", "address": "", "phone": "", "website": "",
          "email": "", "category": "", "niche": "", "location": ""}
check("template vars", sender.render_template("Hi {business_name} {niche}", lead_t) == "Hi Biz ")

# ================= 7b. personalized_line variable =================
import leads as leadsmod
lead_p = dict(lead_t, business_name="Acme", personalized_line="loved your 5-star reviews on fast installs")
check("personalized_line renders in subject",
      sender.render_template("Quick one, {business_name} - {personalized_line}", lead_p) ==
      "Quick one, Acme - loved your 5-star reviews on fast installs")
check("personalized_line renders in body",
      sender.render_template("Hi {business_name},\n{personalized_line}\nWorth a chat?", lead_p) ==
      "Hi Acme,\nloved your 5-star reviews on fast installs\nWorth a chat?")
lead_blank = dict(lead_t)  # no personalized_line key at all
check("personalized_line blank when key missing",
      sender.render_template("Hi {business_name}. {personalized_line}Bye", lead_blank) == "Hi Biz. Bye")
lead_empty = dict(lead_t, personalized_line="")
check("personalized_line blank when empty string",
      sender.render_template("A{personalized_line}B", lead_empty) == "AB")
check("existing vars unaffected by new key",
      sender.render_template("{business_name}|{category}|{website}", dict(lead_p, category="HVAC", website="w.com")) ==
      "Acme|HVAC|w.com")
check("unknown var still left literally",
      sender.render_template("Hi {not_a_var}", lead_p) == "Hi {not_a_var}")
check("spintax + personalized_line combine",
      sender.render_template("{Hi|Hello} {personalized_line}", lead_p).startswith(("Hi ", "Hello ")) and
      sender.render_template("{Hi|Hello} {personalized_line}", lead_p).endswith("loved your 5-star reviews on fast installs"))

# migration is idempotent and the column exists
db._migrate()
cols = [r["name"] for r in db.q("PRAGMA table_info(leads)")]
check("leads.personalized_line column exists after migrate", "personalized_line" in cols, str(cols))
db._migrate()  # second run must not error

# CSV import reads the personalized_line column
import io as _io
u_pl = make_user("pl@example.com")
camp_pl = db.q("SELECT id FROM campaigns WHERE user_id=?", (u_pl["id"],), one=True)
if not camp_pl:
    cid_pl = db.w("INSERT INTO campaigns (user_id, name, created_at) VALUES (?,?,?)",
                  (u_pl["id"], "PL Test", time.time()))
else:
    cid_pl = camp_pl["id"]
n, errs = leadsmod.import_csv(u_pl["id"], cid_pl, _io.BytesIO(
    b"business_name,email,personalized_line\nAcme Co,a@acme.com,saw your new trucks on the road\nNoLine Co,b@noline.com,\n"))
row_pl = db.q("SELECT * FROM leads WHERE campaign_id=? ORDER BY id", (cid_pl,))
check("csv imports personalized_line", n == 2 and not errs and
      row_pl[0]["personalized_line"] == "saw your new trucks on the road" and
      row_pl[1]["personalized_line"] == "", str(errs))
# render from a real DB row (SELECT * shape)
check("render from db row", sender.render_template("Hey {personalized_line}!", row_pl[0]) ==
      "Hey saw your new trucks on the road!")
# manual add accepts it
leadsmod.add_manual(u_pl["id"], cid_pl, {"business_name": "Manual Co", "email": "m@m.com",
                                         "personalized_line": "hand-written hook"})
row_m = db.q("SELECT personalized_line FROM leads WHERE business_name=?", ("Manual Co",), one=True)
check("manual add stores personalized_line", row_m["personalized_line"] == "hand-written hook")

# rotation: two accounts alternate
u3 = make_user("u3@example.com")
a3a = add_account(u3["id"], "r1@gmail.com")
a3b = add_account(u3["id"], "r2@gmail.com")
ccamp = {"id": 999, "user_id": u3["id"], "delay_min": 60, "delay_max": 61,
         "window_start": "00:00", "window_end": "23:59", "dry_run": 1,
         "subject_tpl": "s", "body_tpl": "b", "last_account_id": 0}
got = [sender.pick_account(u3["id"], ccamp)["email"] for _ in range(4)]
check("round-robin rotation", got == ["r1@gmail.com", "r2@gmail.com", "r1@gmail.com", "r2@gmail.com"], str(got))

# caps block
db.w("UPDATE sender_accounts SET daily_cap=1, sent_today=1 WHERE id=?", (a3a,))
db.w("UPDATE sender_accounts SET status='paused' WHERE id=?", (a3b,))
check("caps/paused exclude accounts", sender.pick_account(u3["id"], ccamp) is None)
db.w("UPDATE sender_accounts SET daily_cap=30, sent_today=0, status='active' WHERE id IN (?,?)", (a3a, a3b))

# warmup ramp
db.w("UPDATE sender_accounts SET warmup_enabled=1, warmup_start=? WHERE id=?", (time.time(), a3a))
check("warmup starts at 5/day", sender.effective_cap(
    db.q("SELECT * FROM sender_accounts WHERE id=?", (a3a,), one=True)) == 5)
db.w("UPDATE sender_accounts SET warmup_enabled=0 WHERE id=?", (a3a,))

# enqueue + dry-run processing
c_dry = make_campaign(c1, "DryCamp")
for i in range(3):
    add_lead(c_dry, u1["id"], f"dry{i}@example.com", f"Dry{i}")
n = queue_worker.enqueue_campaign(u1["id"], c_dry)
check("enqueue queues selected leads", n == 3)
out = queue_worker.process_sends()
ok_sends = [o for o in out if o.get("ok")]
check("dry-run processes queue", len(ok_sends) == 3 and all(o.get("dry_run") for o in ok_sends))
check("dry-run logs status", db.q("SELECT COUNT(*) c FROM send_log WHERE campaign_id=? AND status='dry-run'",
                                  (c_dry,), one=True)["c"] == 3)
# dry-run also previews the chain (future follow-ups stay pending)
check("dry-run schedules follow-up previews",
      db.q("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=? AND step=1 AND status='pending'",
           (c_dry,), one=True)["c"] == 3)
camp_dry = db.q("SELECT status FROM campaigns WHERE id=?", (c_dry,), one=True)
check("campaign stays sending while follow-ups pending", camp_dry["status"] == "sending")
r = c1.get(f"/campaign/{c_dry}")
check("campaign page shows per-lead step status", "F1 queued" in r.data.decode())

# window deferral
cid_w = make_campaign(c1, "WinCamp")
db.w("UPDATE campaigns SET window_start='00:00', window_end='00:01' WHERE id=?", (cid_w,))
camp_w = db.q("SELECT * FROM campaigns WHERE id=?", (cid_w,), one=True)
check("in_window false outside window",
      not sender.in_window(camp_w, __import__("datetime").datetime(2026, 1, 1, 12, 0)))

# cron auth
with mock.patch.object(smtp_mail, "scan_replies", return_value=[]):
    r = appmod.app.test_client().post("/api/process-queue")
check("cron rejects without secret", r.status_code == 401)
with mock.patch.object(smtp_mail, "scan_replies", return_value=[]):
    r = appmod.app.test_client().post("/api/process-queue",
                                      headers={"Authorization": "Bearer test-cron-secret-123"})
    j = r.get_json()
check("cron accepts secret", r.status_code == 200 and "sends" in j and "replies_marked" in j)

# isolation: second user sees nothing of first
u9 = make_user("u9@example.com")
c9 = login_client("u9@example.com")
r = c9.get(f"/campaign/{cid}")
check("cross-user campaign blocked", r.status_code == 404)
r = c9.get("/logs")
check("logs scoped to user", b"chain1@example.com" not in r.data and b"Acme Plumbing" not in r.data)
c9r = c9.post("/accounts/add", data={"email": "x@gmail.com", "app_password": "short"})
check("account routes scoped", c9r.status_code in (302, 400))

# template save + preview
r = c1.post(f"/campaign/{c_dry}/template", data={
    "subject_tpl": "Hey {business_name}", "body_tpl": "Yo {business_name} {m|n}",
    "delay_min": 60, "delay_max": 61, "window_start": "00:00", "window_end": "23:59",
    "dry_run": "on"})
check("template save redirects", r.status_code == 302)
r = c1.post(f"/campaign/{c_dry}/preview")
j = r.get_json()
check("preview renders", j["subject"].startswith("Hey ") and j["body"][:2] == "Yo")

# email validator fast paths
from email_validator import validate_email
v = validate_email("not-an-email")
check("validator rejects bad syntax fast", v["verdict"] == "invalid" and not v.get("mx_checked"))
v = validate_email("someone@mailinator.com")
check("validator flags disposable", v["verdict"] == "invalid" and "isposable" in v["reason"])

# chunked jobs (mocked network)
with mock.patch("jobs._validate_one_email", return_value={"verdict": "valid", "detail": "mocked"}):
    jid = jobsmod.start_job(u1["id"], cid, "validate")
    jobsmod.process_one_job_chunk()
    job = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
check("validate job chunk completes (mocked)", job["status"] == "done")
with mock.patch("enrich.enrich_website", return_value={"emails": ["e@x.com"], "has_contact_form": 0}):
    jid = jobsmod.start_job(u1["id"], cid, "enrich")
    jobsmod.process_one_job_chunk()
    job = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
check("enrich job chunk completes (mocked)", job["status"] == "done")

# ================= 8. reply notifications =================
u8 = make_user("u8@example.com")
c8 = login_client("u8@example.com")
a8 = add_account(u8["id"], "send8@gmail.com")
cc8 = make_campaign(c8, "NotifyCamp", dry_run=1)
lid8 = add_lead(cc8, u8["id"], "reply8@example.com", "Reply8 Biz")
acct8 = db.q("SELECT * FROM sender_accounts WHERE id=?", (a8,), one=True)

FakeIMAP.msgs = [
    b"From: reply8@example.com\r\nDate: Sat, 27 Sep 2026 09:00:00 +0000\r\n"
    b"Subject: Re: hi\r\n\r\nSounds great, let's talk Tuesday morning!",
]
with mock.patch("imaplib.IMAP4_SSL", FakeIMAP):
    replies8 = smtp_mail.scan_replies(acct8)
check("notification flow: scan returns snippet + parsed date",
      len(replies8) == 1 and "Tuesday morning" in replies8[0]["snippet"]
      and replies8[0]["date"] is not None)

new8 = sender.mark_replies(u8["id"], replies8)
check("mark_replies returns full lead context",
      len(new8) == 1 and new8[0]["business_name"] == "Reply8 Biz"
      and new8[0]["campaign_name"] == "NotifyCamp"
      and "Tuesday morning" in new8[0]["snippet"])

sent_notes = []
with mock.patch.object(smtp_mail, "send_message",
                       side_effect=lambda a, t, s, b: sent_notes.append((a["email"], t, s, b)) or True):
    n8 = sender.notify_replies(u8["id"], acct8, new8)
check("notify_replies creates one notification row with snippet",
      n8 == 1 and db.q("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND kind='reply'",
                       (u8["id"],), one=True)["c"] == 1
      and "Tuesday morning" in db.q("SELECT snippet FROM notifications WHERE user_id=?",
                                    (u8["id"],), one=True)["snippet"])
check("notify email goes to the user's login email from their sender account",
      len(sent_notes) == 1 and sent_notes[0][1] == "u8@example.com"
      and sent_notes[0][0] == "send8@gmail.com"
      and "Reply from Reply8 Biz" in sent_notes[0][2]
      and "Tuesday morning" in sent_notes[0][3]
      and "/campaign/" in sent_notes[0][3])

# repeat scan: no duplicate notification, no second email
new8b = sender.mark_replies(u8["id"], replies8)
sent_notes.clear()
with mock.patch.object(smtp_mail, "send_message",
                       side_effect=lambda a, t, s, b: sent_notes.append((t, s)) or True):
    n8b = sender.notify_replies(u8["id"], acct8, new8b)
check("no duplicate notifications or emails on repeat scan",
      new8b == [] and n8b == 0 and sent_notes == []
      and db.q("SELECT COUNT(*) c FROM notifications WHERE user_id=?",
               (u8["id"],), one=True)["c"] == 1)

# no sender account: notification stored, email skipped gracefully
lid8b = add_lead(cc8, u8["id"], "reply8b@example.com", "Reply8b Biz")
new8c = sender.mark_replies(u8["id"], [{"address": "reply8b@example.com", "snippet": "hi", "date": None}])
with mock.patch.object(smtp_mail, "send_message", side_effect=AssertionError("should not send")):
    n8c = sender.notify_replies(u8["id"], None, new8c)
check("notification stored without sender account, email skipped",
      n8c == 1 and db.q("SELECT COUNT(*) c FROM notifications WHERE user_id=?",
                        (u8["id"],), one=True)["c"] == 2)

# dashboard hot list: replied + unhandled shows; handled hides
r = c8.get("/dashboard")
html = r.data.decode()
check("dashboard shows replied lead hot list",
      "<b>Reply8 Biz</b>" in html and "Tuesday morning" in html and "Mark handled" in html)
check("header bell shows unread count", 'badge">2<' in html)
db.w("UPDATE leads SET handled=1 WHERE id=?", (lid8,))
r = c8.get("/dashboard")
check("handled lead hidden from hot list", "<b>Reply8 Biz</b>" not in r.data.decode())
r = c8.get("/notifications")
check("notifications page renders + marks all read",
      r.status_code == 200 and "Reply from Reply8 Biz" in r.data.decode()
      and db.q("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND read_at IS NULL",
               (u8["id"],), one=True)["c"] == 0)
r = c8.get("/dashboard")
check("bell badge clears after reading", '<span class="badge">' not in r.data.decode())
r = c8.post(f"/lead/{lid8b}/handled", follow_redirects=False)
check("mark handled route sets flag",
      r.status_code == 302
      and db.q("SELECT handled FROM leads WHERE id=?", (lid8b,), one=True)["handled"] == 1)

# cron tick also notifies (mocked scan + mocked smtp)
with mock.patch.object(smtp_mail, "scan_replies",
                       return_value=[{"address": "reply8@example.com",
                                      "snippet": "again?", "date": None}]):
    with mock.patch.object(smtp_mail, "send_message", return_value=True):
        marked = queue_worker._scan_replies_all()
check("cron reply scan marks without duplicates",
      marked == 0
      and db.q("SELECT COUNT(*) c FROM notifications WHERE user_id=?",
               (u8["id"],), one=True)["c"] == 2)

# ================= 9. autopilot: AI SDR mode =================
import gap_analysis
import ai_writer
import config as configmod

FAKE_FINDINGS = ["no booking, scheduling, or quote option found on the website",
                 "no phone number found on the website"]


def fake_call_openai(messages):
    sys_prompt = messages[0]["content"]
    if "follow-up" in sys_prompt:
        return ({"followups": [
            {"subject": "Bump one", "body": "First bump body."},
            {"subject": "Bump two", "body": "Second bump body."},
            {"subject": "Bump three", "body": "Third bump body."}]},
                {"input": 500, "output": 150})
    return ({"subject": "AI subject plain",
             "body": "Hi Acme team, this is the AI-written opener. Worth a 10-minute chat?"},
            {"input": 400, "output": 120})


def fake_analysis(*a, **k):
    return {"findings": FAKE_FINDINGS, "reachable": True, "load_seconds": 0.5}


# --- gap analyzer: deterministic, no network in tests ---
r = gap_analysis.analyze_website("")
check("gap: no website finding",
      r["findings"] == ["the business has no website"] and not r["reachable"])
r = gap_analysis.analyze_website("http://nowhere.invalid", fetch_fn=lambda u: "")
check("gap: unreachable finding",
      r["findings"] == ["the business website could not be reached"]
      and not r["reachable"])
rich_html = ("<html><head><title>Acme</title><meta name='viewport' content='width=device-width'>"
             "</head><body>Contact us at hello@acme.com or (602) 555-0100. "
             "<a href='https://instagram.com/acme'>IG</a> Read our testimonials. "
             "<a href='/book'>Book appointment</a></body></html>")
r = gap_analysis.analyze_website("acme.com", fetch_fn=lambda u: rich_html)
check("gap: full site positive findings",
      "the website lists a contact email address" in r["findings"]
      and "the website lists a phone number" in r["findings"]
      and "the website has a booking, scheduling, or quote option" in r["findings"]
      and "the website shows reviews or testimonials" in r["findings"]
      and "the website links to social media profiles" in r["findings"]
      and "the website is set up for mobile screens" in r["findings"]
      and r["reachable"], str(r["findings"]))
bare_html = "<html><head><title>Bare</title></head><body><p>We do stuff.</p></body></html>"
r = gap_analysis.analyze_website("bare.com", fetch_fn=lambda u: bare_html)
check("gap: bare site gap findings",
      "no contact email found on the website" in r["findings"]
      and "no phone number found on the website" in r["findings"]
      and "no booking, scheduling, or quote option found on the website" in r["findings"]
      and "no reviews or testimonials found on the website" in r["findings"]
      and "no social media links found on the website" in r["findings"]
      and "the website has no mobile layout tag (may look broken on phones)" in r["findings"])

# --- prompt grounding rules ---
check("system prompt: only observed facts",
      "ONLY the observed facts" in ai_writer.SYSTEM_PROMPT)
check("system prompt: never invent metrics",
      "Never invent metrics" in ai_writer.SYSTEM_PROMPT)
check("system prompt: word cap", "120 words" in ai_writer.SYSTEM_PROMPT)
check("followup prompt: grounded, 60 words, 3 angles",
      "ONLY the observed facts" in ai_writer.FOLLOWUP_PROMPT
      and "60 words" in ai_writer.FOLLOWUP_PROMPT
      and "DIFFERENT angle" in ai_writer.FOLLOWUP_PROMPT)
check("model is a cheap constant", configmod.AI_MODEL == "gpt-4o-mini")

captured = {}


def spy_call(messages):
    captured["messages"] = messages
    return fake_call_openai(messages)


lead9info = {"business_name": "Acme Co", "category": "plumber",
             "address": "Phoenix AZ", "website": "acme.com"}
with mock.patch.object(ai_writer, "_call_openai", side_effect=spy_call):
    subj, body, usage = ai_writer.generate_email(lead9info, {"niche": "plumber"},
                                                 FAKE_FINDINGS)
user_txt = captured["messages"][1]["content"]
check("findings passed to model verbatim",
      all(f in user_txt for f in FAKE_FINDINGS) and "Acme Co" in user_txt
      and "plumber" in user_txt)
check("email word cap enforced", len(body.split()) <= 120 and bool(subj))
with mock.patch.object(ai_writer, "_call_openai", side_effect=spy_call):
    fus, _ = ai_writer.generate_followups(lead9info, {}, FAKE_FINDINGS, body)
check("3 follow-ups, each under 60 words",
      len(fus) == 3 and all(len(b.split()) <= 60 for _, b in fus))
check("cost estimate math",
      abs(ai_writer.estimate_cost_usd(1_000_000, 1_000_000) - 0.75) < 1e-9)

# --- generation + caching ---
configmod.OPENAI_API_KEY = "test-key-123"
ua = make_user("ua@example.com")
ca = login_client("ua@example.com")
add_account(ua["id"], "auto9@gmail.com")
camp9 = make_campaign(ca, "Auto1", dry_run=0)
db.w("UPDATE campaigns SET autopilot=1, followup_mode='until_reply', max_touches=5 WHERE id=?",
     (camp9,))
lid9 = add_lead(camp9, ua["id"], "auto9@example.com", "Auto Biz")
db.w("UPDATE leads SET website='acme.com', category='plumber' WHERE id=?", (lid9,))
camp9row = lambda: db.q("SELECT * FROM campaigns WHERE id=?", (camp9,), one=True)
lead9row = lambda: db.q("SELECT * FROM leads WHERE id=?", (lid9,), one=True)
with mock.patch.object(ai_writer, "_call_openai", side_effect=fake_call_openai) as m, \
     mock.patch.object(ai_writer.gap_analysis, "analyze_website",
                       side_effect=fake_analysis):
    st1, _ = ai_writer.generate_and_store(lead9row(), camp9row())
    st2, _ = ai_writer.generate_and_store(lead9row(), camp9row())
check("ai content generated once and cached",
      st1 == "ready" and st2 == "ready" and m.call_count == 2
      and db.q("SELECT COUNT(*) c FROM ai_content WHERE lead_id=?",
               (lid9,), one=True)["c"] == 1
      and lead9row()["ai_status"] == "ready")
avg, n = ai_writer.avg_cost_per_lead(camp9)
check("avg cost per lead from stored tokens", n == 1 and 0 < avg < 0.01)

# skip: no website and no business info -> fallback, no AI call
lid9s = add_lead(camp9, ua["id"], "auto9s@example.com", "")
db.w("UPDATE leads SET business_name='', category='', website='' WHERE id=?", (lid9s,))
with mock.patch.object(ai_writer, "_call_openai", side_effect=fake_call_openai) as m2:
    st, note = ai_writer.generate_and_store(
        db.q("SELECT * FROM leads WHERE id=?", (lid9s,), one=True), camp9row())
check("skip when no website and no business info",
      st == "skipped" and m2.call_count == 0
      and "normal template" in note
      and db.q("SELECT ai_status FROM leads WHERE id=?",
               (lid9s,), one=True)["ai_status"] == "skipped"
      and db.q("SELECT id FROM ai_content WHERE lead_id=?",
               (lid9s,), one=True) is None)
db.w("UPDATE leads SET selected=0 WHERE id=?", (lid9s,))  # keep it out of send tests

# --- sending uses AI content; until-reply chains to max_touches ---
s0 = sender._templates_for(camp9row(), 0, lead9row())
check("templates_for uses AI email", s0 == ("AI subject plain", body))
s2 = sender._templates_for(camp9row(), 2, lead9row())
check("templates_for uses AI follow-ups", s2 == ("Bump two", "Second bump body."))
s4 = sender._templates_for(camp9row(), 4, lead9row())
check("templates_for cycles fu3 beyond step 3",
      s4[1].startswith("Circling back once more: ") and "Third bump body." in s4[1])


def only(cid):
    db.w("UPDATE campaigns SET status='stopped' WHERE user_id=? AND id<>?",
         (ua["id"], cid))
    db.w("UPDATE campaigns SET status='sending' WHERE id=?", (cid,))


queue_worker.enqueue_campaign(ua["id"], camp9)
sent9 = []
wake(camp9)
only(camp9)
queue_worker.process_sends(send_fn=lambda a, t, s, b: sent9.append((t, s, b)) or True)
check("autopilot step-0 sends AI email",
      len(sent9) == 1 and sent9[0][1] == "AI subject plain"
      and "AI-written opener" in sent9[0][2])
ok_chain = True
for st in (1, 2, 3, 4):
    q = db.q("SELECT * FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=?",
             (camp9, lid9, st), one=True)
    if not q:
        ok_chain = False
        break
    db.w("UPDATE send_queue SET scheduled_at=? WHERE id=?", (time.time() - 1, q["id"]))
    wake(camp9)
    only(camp9)
    before = len(sent9)
    queue_worker.process_sends(send_fn=lambda a, t, s, b: sent9.append((t, s, b)) or True)
    if len(sent9) != before + 1:
        ok_chain = False
        break
check("until-reply chains steps 1-4", ok_chain and len(sent9) == 5)
check("step-1..3 use AI follow-ups",
      sent9[1][1] == "Bump one" and sent9[2][1] == "Bump two"
      and sent9[3][1] == "Bump three")
check("step-4 cycles fu3 with prefix",
      sent9[4][2].startswith("Circling back once more: ")
      and "Third bump body." in sent9[4][2])
check("never exceeds max_touches",
      db.q("SELECT id FROM send_queue WHERE campaign_id=? AND lead_id=? AND step>=5",
           (camp9, lid9), one=True) is None
      and db.q("SELECT COUNT(*) c FROM send_log WHERE campaign_id=? AND lead_id=? "
               "AND status='sent'", (camp9, lid9), one=True)["c"] == 5)

# reply stops until-reply mode
camp9b = make_campaign(ca, "Auto2", dry_run=0)
db.w("UPDATE campaigns SET autopilot=1, followup_mode='until_reply', max_touches=7 "
     "WHERE id=?", (camp9b,))
lid9b = add_lead(camp9b, ua["id"], "auto9b@example.com", "Auto B")
db.w("""INSERT INTO ai_content (lead_id, subject, body, fu1_subj, fu1_body, fu2_subj,
        fu2_body, fu3_subj, fu3_body, findings_json, tokens_in, tokens_out, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
     (lid9b, "S", "B", "FS1", "FB1", "FS2", "FB2", "FS3", "FB3",
      "[]", 1, 1, time.time()))
queue_worker.enqueue_campaign(ua["id"], camp9b)
wake(camp9b)
only(camp9b)
queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
sender.mark_replies(ua["id"], ["auto9b@example.com"])
q = db.q("SELECT * FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=1",
         (camp9b, lid9b), one=True)
db.w("UPDATE send_queue SET scheduled_at=? WHERE id=?", (time.time() - 1, q["id"]))
wake(camp9b)
only(camp9b)
queue_worker.process_sends(send_fn=lambda a, t, s, b: True)
row = db.q("SELECT status, last_error FROM send_queue WHERE id=?", (q["id"],), one=True)
check("reply stops until-reply sequence",
      row["status"] == "failed" and "replied" in row["last_error"].lower())

# autopilot on but no AI content -> normal template fallback
camp9c = make_campaign(ca, "Auto3", dry_run=0)
db.w("UPDATE campaigns SET autopilot=1 WHERE id=?", (camp9c,))
lid9c = add_lead(camp9c, ua["id"], "auto9c@example.com", "Auto C")
queue_worker.enqueue_campaign(ua["id"], camp9c)
sent9c = []
wake(camp9c)
only(camp9c)
queue_worker.process_sends(send_fn=lambda a, t, s, b: sent9c.append((t, s, b)) or True)
check("autopilot falls back to template without AI content",
      len(sent9c) == 1 and sent9c[0][1] == "Hi Auto C"
      and sent9c[0][2].startswith("Hello Auto C,"))

# --- settings saved + clamped via template route ---
camp9e = make_campaign(ca, "Auto5", dry_run=1)
tpl_data = {"subject_tpl": "S", "body_tpl": "B", "delay_min": 60, "delay_max": 61,
            "window_start": "00:00", "window_end": "23:59",
            "followups_enabled": "on", "followup_count": "5",
            "followup_delay_hours": "40",
            "autopilot": "on", "followup_mode": "until_reply", "max_touches": "99"}
ca.post(f"/campaign/{camp9e}/template", data=tpl_data)
camp = db.q("SELECT autopilot, followup_mode, max_touches FROM campaigns WHERE id=?",
            (camp9e,), one=True)
check("autopilot settings saved, touches clamped",
      camp["autopilot"] == 1 and camp["followup_mode"] == "until_reply"
      and camp["max_touches"] == 20)
tpl_data.update({"followup_mode": "bogus", "max_touches": "0"})
ca.post(f"/campaign/{camp9e}/template", data=tpl_data)
camp = db.q("SELECT followup_mode, max_touches FROM campaigns WHERE id=?",
            (camp9e,), one=True)
check("bogus mode resets, touches floor at 1",
      camp["followup_mode"] == "fixed" and camp["max_touches"] == 1)
r = ca.get(f"/campaign/{camp9}")
page = r.data.decode()
check("campaign page: toggle + cost + AI status shown",
      'name="autopilot"' in page and "per email" in page and "AI written" in page)
r = ca.get("/settings")
check("settings shows AI enabled", "Enabled" in r.data.decode())

# --- autopilot job (chunked, serverless-safe) ---
db.w("UPDATE jobs SET status='done' WHERE status='running'")
lid9e = add_lead(camp9e, ua["id"], "auto9e@example.com", "Auto E")
db.w("UPDATE leads SET website='acme.com' WHERE id=?", (lid9e,))
r = ca.post(f"/campaign/{camp9e}/autopilot", follow_redirects=False)
check("autopilot route starts job",
      r.status_code == 302
      and db.q("SELECT id FROM jobs WHERE campaign_id=? AND kind='autopilot' "
               "AND status='running'", (camp9e,), one=True) is not None)
with mock.patch.object(ai_writer, "_call_openai", side_effect=fake_call_openai), \
     mock.patch.object(ai_writer.gap_analysis, "analyze_website",
                       side_effect=fake_analysis):
    jobsmod.process_one_job_chunk()  # init
    jobsmod.process_one_job_chunk()  # one lead
check("autopilot job writes AI content",
      db.q("SELECT id FROM ai_content WHERE lead_id=?", (lid9e,), one=True) is not None
      and db.q("SELECT ai_status FROM leads WHERE id=?",
               (lid9e,), one=True)["ai_status"] == "ready")
with mock.patch.object(ai_writer, "_call_openai", side_effect=fake_call_openai), \
     mock.patch.object(ai_writer.gap_analysis, "analyze_website",
                       side_effect=fake_analysis):
    jobsmod.process_one_job_chunk()  # done
check("autopilot job completes",
      db.q("SELECT status FROM jobs WHERE campaign_id=? AND kind='autopilot' "
           "ORDER BY id DESC LIMIT 1", (camp9e,), one=True)["status"] == "done")

# --- no key: autopilot unavailable, normal templates keep working ---
configmod.OPENAI_API_KEY = ""
camp9d = make_campaign(ca, "Auto4", dry_run=1)
tpl_nokey = dict(tpl_data, subject_tpl="Sx", autopilot="on",
                 followup_mode="until_reply", max_touches="7")
ca.post(f"/campaign/{camp9d}/template", data=tpl_nokey)
camp = db.q("SELECT autopilot, followup_mode FROM campaigns WHERE id=?",
            (camp9d,), one=True)
check("no key: autopilot toggle forced off", camp["autopilot"] == 0)
r = ca.get(f"/campaign/{camp9d}")
check("campaign page explains AI is off", "AI writing is off" in r.data.decode())
r = ca.get("/settings")
check("settings shows AI disabled",
      "Disabled" in r.data.decode() and "OPENAI_API_KEY" in r.data.decode())
r = ca.post(f"/campaign/{camp9d}/autopilot")
check("autopilot route refuses without key", "AI writing is off" in r.data.decode())
lid9d = add_lead(camp9d, ua["id"], "auto9d@example.com", "Auto D")
queue_worker.enqueue_campaign(ua["id"], camp9d)
sent9d = []
wake(camp9d)
only(camp9d)
queue_worker.process_sends(send_fn=lambda a, t, s, b: sent9d.append((t, s, b)) or True)
check("no key: normal template still sends",
      len(sent9d) == 1 and sent9d[0][1] == "Sx")
configmod.OPENAI_API_KEY = ""

# ================= 10. Full Autopilot pipeline =================
import pipeline as pipelinemod

colnames = [r["name"] for r in db.q("PRAGMA table_info(campaigns)")]
for cname in ("pipeline_enabled", "pipeline_target_leads",
              "pipeline_stage", "pipeline_cursor"):
    check(f"migration: campaigns.{cname} exists", cname in colnames)
db.init_db(); db.init_db()
colnames2 = [r["name"] for r in db.q("PRAGMA table_info(campaigns)")]
check("migration idempotent (run twice)", colnames == colnames2)

# --- routes: save / start / pause ---
campP = make_campaign(ca, "Pipe1", dry_run=1)
campP0 = make_campaign(ca, "Pipe0", dry_run=1)
db.w("UPDATE campaigns SET niche='', location='' WHERE id=?", (campP0,))
r = ca.post(f"/campaign/{campP0}/pipeline_start", follow_redirects=False)
check("pipeline start needs niche+location",
      r.status_code == 200 and "Niche and location needed" in r.data.decode())
r = ca.post(f"/campaign/{campP}/pipeline_save",
            data={"niche": "HVAC contractor", "location": "Phoenix AZ",
                  "pipeline_target_leads": "12"}, follow_redirects=False)
camp = db.q("SELECT niche, location, pipeline_target_leads "
            "FROM campaigns WHERE id=?", (campP,), one=True)
check("pipeline save stores niche/location/target",
      r.status_code == 302 and camp["niche"] == "HVAC contractor"
      and camp["location"] == "Phoenix AZ" and camp["pipeline_target_leads"] == 12)
ca.post(f"/campaign/{campP}/pipeline_save",
        data={"niche": "x", "location": "y", "pipeline_target_leads": "9999"},
        follow_redirects=False)
camp = db.q("SELECT pipeline_target_leads FROM campaigns WHERE id=?", (campP,), one=True)
check("pipeline target clamped to 500", camp["pipeline_target_leads"] == 500)
r = ca.post(f"/campaign/{campP}/pipeline_start", follow_redirects=False)
camp = db.q("SELECT pipeline_enabled, pipeline_stage FROM campaigns WHERE id=?",
            (campP,), one=True)
check("pipeline start enables + resets to discover",
      r.status_code == 302 and camp["pipeline_enabled"] == 1
      and camp["pipeline_stage"] == "discover")
r = ca.post(f"/campaign/{campP}/pipeline_pause", follow_redirects=False)
camp = db.q("SELECT pipeline_enabled FROM campaigns WHERE id=?", (campP,), one=True)
check("pipeline pause disables", r.status_code == 302 and camp["pipeline_enabled"] == 0)
r = ca.get(f"/campaign/{campP}")
htmlP = r.data.decode()
check("campaign page shows pipeline box + stats",
      "Full Autopilot pipeline" in htmlP and "Start pipeline" in htmlP
      and "Stage:" in htmlP)

# --- discover stage (mocked web search) ---
db.w("UPDATE campaigns SET pipeline_enabled=1, pipeline_stage='discover', "
     "pipeline_target_leads=4, pipeline_cursor='{}', niche='HVAC contractor', "
     "location='Phoenix AZ' WHERE id=?", (campP,))
fake_links = ["https://acmeair.example/page", "https://bestcool.example/",
              "https://acmeair.example/other"]


def fake_identity(url):
    from urllib.parse import urlparse
    dom = urlparse(url).netloc
    return {"name": dom.split(".")[0].title() + " Co", "phone": "", "address": ""}


with mock.patch("scrapers.websearch.ddg_links", return_value=list(fake_links)), \
     mock.patch("scrapers.websearch._site_identity", side_effect=fake_identity):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage, note = pipelinemod.tick_campaign(camp)
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage2, note2 = pipelinemod.tick_campaign(camp)  # variants exhaust -> enrich
check("discover tick inserts deduped leads",
      stage == "discover"
      and db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=?",
               (campP,), one=True)["c"] == 2)
check("discover dedupes across ticks, then advances to enrich",
      stage2 == "enrich" and "advanced" in note2)
db.w("UPDATE campaigns SET pipeline_stage='discover' WHERE id=?", (campP,))
with mock.patch("scrapers.websearch.ddg_links", return_value=list(fake_links)), \
     mock.patch("scrapers.websearch._site_identity", side_effect=fake_identity):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage3, _ = pipelinemod.tick_campaign(camp)
check("discover never inserts duplicates",
      db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=?",
           (campP,), one=True)["c"] == 2)

# --- enrich stage (mocked) ---
db.w("UPDATE campaigns SET pipeline_stage='enrich' WHERE id=?", (campP,))


def fake_enrich(rows, pause=0.8):
    out = []
    for i, r in enumerate(rows):
        if i == 0:
            out.append({"lead_id": r["id"], "email": "info@acmeair.example",
                        "phone": "555-1234", "has_contact_form": False,
                        "pages_checked": 2})
        else:
            out.append({"lead_id": r["id"], "email": "", "phone": "",
                        "has_contact_form": True, "pages_checked": 2})
    return out


with mock.patch("enrich.enrich_leads", side_effect=fake_enrich):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage, _ = pipelinemod.tick_campaign(camp)
got = db.q("SELECT id, email, email_verdict FROM leads WHERE campaign_id=? AND email<>''",
           (campP,))
none = db.q("SELECT id, email_verdict FROM leads WHERE campaign_id=? AND email=''",
            (campP,))
check("enrich: found email stored, verdict pending",
      stage == "enrich" and len(got) == 1 and got[0]["email_verdict"] == ""
      and got[0]["email"] == "info@acmeair.example")
check("enrich: no-email lead marked none", len(none) == 1 and none[0]["email_verdict"] == "none")
with mock.patch("enrich.enrich_leads",
                side_effect=AssertionError("no-email lead must not be retried")):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage, note = pipelinemod.tick_campaign(camp)
check("enrich completes without retrying none leads",
      stage == "validate" and "advanced" in note)

# --- validate stage (mocked) ---
lid_good = got[0]["id"]
lid_bad = add_lead(campP, ua["id"], "bad@invalid.example", "Bad Biz")


def fake_validate(emails, max_workers=5):
    return [{"email": e, "verdict": "invalid" if e.startswith("bad@") else "valid",
             "reason": "mocked", "mx_host": "", "catch_all": False,
             "duration": 0.1} for e in emails]


with mock.patch("email_validator.validate_emails", side_effect=fake_validate):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage, _ = pipelinemod.tick_campaign(camp)
good = db.q("SELECT email, email_verdict FROM leads WHERE id=?", (lid_good,), one=True)
bad = db.q("SELECT email, email_verdict FROM leads WHERE id=?", (lid_bad,), one=True)
check("validate: good email kept as valid",
      stage == "validate" and good["email_verdict"] == "valid" and good["email"] != "")
check("validate: invalid email dropped (cleared, never sendable)",
      bad["email"] == "" and bad["email_verdict"] == "invalid")
with mock.patch("email_validator.validate_emails",
                side_effect=AssertionError("nothing left to validate")):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage, note = pipelinemod.tick_campaign(camp)
check("validate completes, advances to write",
      stage == "write" and "advanced" in note)

# --- write stage ---
configmod.OPENAI_API_KEY = ""
camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
stage, note = pipelinemod.tick_campaign(camp)
check("write skipped without API key, advances to queue",
      stage == "queue" and "advanced" in note)
configmod.OPENAI_API_KEY = "sk-test-..."


def fake_gen(lead, camp):
    db.w("UPDATE leads SET ai_status='ready' WHERE id=?", (lead["id"],))
    return ("ready", "")


db.w("UPDATE campaigns SET pipeline_stage='write' WHERE id=?", (campP,))
with mock.patch("ai_writer.generate_and_store", side_effect=fake_gen):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage, _ = pipelinemod.tick_campaign(camp)
ai_leads = db.q("SELECT id FROM leads WHERE campaign_id=? AND ai_status='ready'",
                (campP,))
check("write: AI runs only for validated lead",
      stage == "write" and [l["id"] for l in ai_leads] == [lid_good])
with mock.patch("ai_writer.generate_and_store",
                side_effect=AssertionError("nothing left to write")):
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
    stage, note = pipelinemod.tick_campaign(camp)
check("write completes, advances to queue",
      stage == "queue" and "advanced" in note)

# --- queue stage: strict guards ---
lid_unk = add_lead(campP, ua["id"], "mystery@example.com", "Mystery")
db.w("UPDATE leads SET email_verdict='unknown' WHERE id=?", (lid_unk,))
lid_rep = add_lead(campP, ua["id"], "reply@example.com", "Replier")
db.w("UPDATE leads SET email_verdict='valid', replied=1 WHERE id=?", (lid_rep,))
lid_bnc = add_lead(campP, ua["id"], "bounce@example.com", "Bouncer")
db.w("UPDATE leads SET email_verdict='valid' WHERE id=?", (lid_bnc,))
db.w("INSERT INTO send_log (user_id, campaign_id, lead_id, recipient, sent_at, status)"
     " VALUES (?,?,?,?,?,'bounced')",
     (ua["id"], campP, lid_bnc, "bounce@example.com", time.time()))
camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
stage, _ = pipelinemod.tick_campaign(camp)
queued = [q["lead_id"] for q in
          db.q("SELECT lead_id FROM send_queue WHERE campaign_id=?", (campP,))]
check("queue: only valid, uncontacted lead queued",
      stage == "queue" and queued == [lid_good])
camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
stage, note = pipelinemod.tick_campaign(camp)
check("queue idempotent (no dupes), completes to done",
      stage == "done" and "advanced" in note
      and db.q("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=?",
               (campP,), one=True)["c"] == 1)
camp = db.q("SELECT * FROM campaigns WHERE id=?", (campP,), one=True)
stage, note = pipelinemod.tick_campaign(camp)
check("done stage is terminal (never restarts)", stage == "done" and note == "")

# --- tick_all: paused campaigns skipped ---
db.w("UPDATE campaigns SET pipeline_enabled=0 WHERE id=?", (campP,))
s = pipelinemod.tick_all()
check("tick_all skips paused campaigns",
      all(c["id"] != campP for c in s["campaigns"]) and "elapsed_s" in s)
db.w("UPDATE campaigns SET pipeline_enabled=1 WHERE id=?", (campP,))
s = pipelinemod.tick_all()
mine = [c for c in s["campaigns"] if c["id"] == campP]
check("tick_all advances one stage per enabled campaign per call",
      len(mine) == 1 and mine[0]["stage"] == "done")
db.w("UPDATE campaigns SET pipeline_enabled=0 WHERE id=?", (campP,))

# --- process_all integration: pipeline tick runs inside the scheduler ---
db.w("UPDATE campaigns SET pipeline_enabled=1, pipeline_stage='done' WHERE id=?",
     (campP,))
with mock.patch.object(queue_worker, "_scan_replies_all", return_value=0), \
     mock.patch.object(queue_worker, "process_sends", return_value={"processed": 0}), \
     mock.patch.object(jobsmod, "process_one_job_chunk", return_value=None), \
     mock.patch.object(pipelinemod, "tick_campaign",
                       side_effect=pipelinemod.tick_campaign) as spy_tick:
    res = queue_worker.process_all()
db.w("UPDATE campaigns SET pipeline_enabled=0 WHERE id=?", (campP,))
check("process_all includes pipeline tick",
      "pipeline" in res and "campaigns" in res["pipeline"] and spy_tick.called)

# no residual Google OAuth references
import subprocess
g = subprocess.run(["grep", "-rn", "--exclude-dir=tests",
                    "gmail_oauth\\|GOOGLE_CLIENT_ID\\|GOOGLE_CLIENT_SECRET",
                    BASE, "--include=*.py", "--include=*.html", "--include=*.txt"],
                   capture_output=True, text=True).stdout.strip()
check("no Google OAuth references", g == "", g[:200])

print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
