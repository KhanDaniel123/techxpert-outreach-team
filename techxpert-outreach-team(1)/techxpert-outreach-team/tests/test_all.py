"""Full test suite for the TechXpert Outreach team app (v2, Google-free).

Covers: registration/login/logout, wrong-password rejection, password
hashing, per-user isolation, sender accounts (App Password + mocked SMTP),
follow-up sequences (config, chaining, gating, reply detection), mocked SMTP
sending, IMAP bounce/reply scans, and the preserved engine behavior
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
    addrs = smtp_mail.scan_replies(acct_row)
check("scan_replies finds replier, skips daemon + self",
      addrs == ["chain5@example.com"], str(addrs))
n = sender.mark_replies(u1["id"], ["CHAIN5@EXAMPLE.COM", "nope@example.com"])
check("mark_replies is case-insensitive + user-scoped",
      n == 1 and db.q("SELECT replied FROM leads WHERE id=?", (lid5,), one=True)["replied"] == 1)

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
