"""Tests for decision-maker enrichment (decision_makers.py + its pipeline,
sender, and queue-worker integration).

Rules under test: verified decision makers only, corroborated per person
against the business's own site/imprint or 2+ independent sources (each
source fetched live and checked for the actual name), only publicly listed
emails (never guessed), no-key graceful pending, per-contact queueing with
same-domain spread and per-contact follow-up chains.

Run: ~/workspace/venvs/shakedown-venv/bin/python tests/test_decision_makers.py
"""
import os
import sys
import time
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DB_PATH = "/tmp/dmtest.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH
from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["CRON_SECRET"] = "test-cron-secret-dm"
os.environ["GEMINI_API_KEY"] = "test-key"

import db
db.init_db()
import decision_makers as dmm
import sender
import queue_worker
import pipeline as pipelinemod

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def make_campaign(uid, name="DMCamp"):
    now = time.time()
    return db.w(
        "INSERT INTO campaigns (user_id, name, dry_run, delay_min, delay_max, "
        "window_start, window_end, subject_tpl, body_tpl, status, created_at, "
        "dm_enabled, dm_max_contacts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, name, 1, 60, 61, "00:00", "23:59", "Hi {business_name}",
         "Hello {contact_name}", "active", now, 1, 3))


def make_lead(cid, uid, name="FitLife Studio", website="https://fitlife.example.com"):
    now = time.time()
    return db.w(
        "INSERT INTO leads (user_id, campaign_id, business_name, website, email, "
        "email_verdict, selected, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uid, cid, name, website, "info@fitlife.example.com", "valid", 1, now))


SITE_HTML = """
<html><body><h1>FitLife Studio</h1>
<p>Owner: Anna Berg</p><p>Managing Director: Anna Berg</p>
<a href="mailto:anna@fitlife.example.com">Anna Berg</a>
</body></html>"""

OTHER_HTML = """
<html><body><h1>Anna Berg - FitLife Studio</h1>
<p>Anna Berg is the owner of FitLife Studio.</p>
</body></html>"""

ANOTHER_HTML = """
<html><body><article>Fitness entrepreneur Anna Berg runs FitLife Studio in Berlin.</article></body></html>"""


def fake_gemini(prompt):
    return ([{
        "name": "Anna Berg",
        "title": "Owner",
        "source_urls": ["https://other.example.com/anna-berg",
                        "https://another.example.com/anna"],
    }, {
        "name": "Ghost Person",   # uncorroborated suggestion -> must be dropped
        "title": "CEO",
        "source_urls": ["https://blog.example.com/random"],
    }], ["other.example.com", "another.example.com", "blog.example.com"])


def fake_fetch(url):
    if "//other.example.com/" in url:
        return OTHER_HTML
    if "//another.example.com/" in url:
        return ANOTHER_HTML
    if "fitlife.example.com" in url:
        return SITE_HTML
    return ""


uid = db.w("INSERT INTO app_users (email, name, password_hash, created_at) VALUES (?,?,?,?)",
           ("dmtest@example.com", "DM Tester", "x", time.time()))

import crypto as cryptomod
db.w("""INSERT INTO sender_accounts
    (user_id, email, password_enc, daily_cap, warmup_enabled, warmup_start,
     status, sent_today, sent_date, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)""",
    (uid, "sender@example.com", cryptomod.encrypt_token("abcdefghijklmnop"), 30, 0,
     time.time(), "active", 0, "", time.time()))

# --- 1. official site keeps the verified person; uncorroborated AI suggestion dropped ---
cid = make_campaign(uid, "DM1")
lid = make_lead(cid, uid)
with mock.patch.object(dmm, "_gemini_call", side_effect=fake_gemini), \
     mock.patch.object(dmm, "_fetch_page", side_effect=fake_fetch):
    status, added = dmm.enrich_lead(
        db.q("SELECT * FROM leads WHERE id=?", (lid,), one=True),
        db.q("SELECT * FROM campaigns WHERE id=?", (cid,), one=True),
        max_contacts=3)
contacts = dmm.get_contacts(lid)
names = [c["name"] for c in contacts]
check("official-site corroboration keeps the verified person", "Anna Berg" in names, str(names))
check("uncorroborated AI suggestion is discarded", "Ghost Person" not in names, str(names))
check("enrich_lead reports done", status == "done", str(status))
anna = [c for c in contacts if c["name"] == "Anna Berg"][0]
check("verified flag set", bool(anna["verified"]) is True)
check("public person-linked email attached", anna["email"] == "anna@fitlife.example.com",
      str(anna["email"]))
check("source url stored", "fitlife.example.com" in (anna["source_url"] or ""),
      str(anna["source_url"]))
check("lead dm_status done", db.q("SELECT dm_status FROM leads WHERE id=?", (lid,),
                                  one=True)["dm_status"] == "done")

# --- 2. two-source rule with per-person fetch: person confirmed on 2 independent pages ---
cid1b = make_campaign(uid, "DM1b")
lid1b = db.w(
    "INSERT INTO leads (user_id, campaign_id, business_name, website, email, email_verdict, "
    "selected, created_at) VALUES (?,?,?,?,?,?,?,?)",
    (uid, cid1b, "NoSite Gym", "", "info@nosite.example.com", "valid", 1, time.time()))


def fake_gemini_nosite(prompt):
    return ([{"name": "Anna Berg", "title": "Owner",
              "source_urls": ["https://other.example.com/anna-berg",
                              "https://another.example.com/anna"]}],
            ["other.example.com", "another.example.com"])


# no website -> skipped (cannot even start); use a website whose own pages name nobody
lid1c = make_lead(cid1b, uid, name="Plain Gym", website="https://plain.example.com")


def fake_fetch_plain(url):
    if "//other.example.com/" in url:
        return OTHER_HTML
    if "//another.example.com/" in url:
        return ANOTHER_HTML
    return "<html><body><h1>Plain Gym</h1><p>Welcome.</p></body></html>"


with mock.patch.object(dmm, "_gemini_call", side_effect=fake_gemini_nosite), \
     mock.patch.object(dmm, "_fetch_page", side_effect=fake_fetch_plain):
    dmm.enrich_lead(db.q("SELECT * FROM leads WHERE id=?", (lid1c,), one=True),
                    db.q("SELECT * FROM campaigns WHERE id=?", (cid1b,), one=True),
                    max_contacts=3)
c1b = dmm.get_contacts(lid1c)
check("person confirmed on 2 fetched independent pages is kept",
      any(c["name"] == "Anna Berg" for c in c1b), str([c["name"] for c in c1b]))
ev = [c for c in c1b if c["name"] == "Anna Berg"][0]
check("evidence url is a confirming page",
      "other.example.com" in (ev["source_url"] or "") or "another.example.com" in (ev["source_url"] or ""),
      str(ev["source_url"]))

# --- 3. only 1 of 2 sources actually names the person -> dropped ---
cid3 = make_campaign(uid, "DM3")
lid3 = make_lead(cid3, uid, name="Plain Gym 3", website="https://plain3.example.com")


def fake_fetch_one(url):
    if "//other.example.com/" in url:
        return OTHER_HTML
    if "//another.example.com/" in url:
        return "<html><body>unrelated page about someone else</body></html>"
    return "<html><body><h1>Plain Gym 3</h1></body></html>"


with mock.patch.object(dmm, "_gemini_call", side_effect=fake_gemini_nosite), \
     mock.patch.object(dmm, "_fetch_page", side_effect=fake_fetch_one):
    dmm.enrich_lead(db.q("SELECT * FROM leads WHERE id=?", (lid3,), one=True),
                    db.q("SELECT * FROM campaigns WHERE id=?", (cid3,), one=True),
                    max_contacts=3)
c3 = dmm.get_contacts(lid3)
check("person named on only 1 of 2 sources is dropped",
      all(c["name"] != "Anna Berg" for c in c3), str([c["name"] for c in c3]))

# --- 4. no fabricated emails: person on site but no public email ---
cid2 = make_campaign(uid, "DM2")
lid2 = make_lead(cid2, uid, name="Silent Gym", website="https://silent.example.com")


def fake_gemini_noemail(prompt):
    return ([{"name": "Silent Sam", "title": "Owner",
              "source_urls": ["https://silent.example.com/about"]}], [])


def fake_fetch_noemail(url):
    return "<html><body><p>Inhaber: Silent Sam</p></body></html>"


with mock.patch.object(dmm, "_gemini_call", side_effect=fake_gemini_noemail), \
     mock.patch.object(dmm, "_fetch_page", side_effect=fake_fetch_noemail):
    dmm.enrich_lead(db.q("SELECT * FROM leads WHERE id=?", (lid2,), one=True),
                    db.q("SELECT * FROM campaigns WHERE id=?", (cid2,), one=True),
                    max_contacts=3)
c2 = dmm.get_contacts(lid2)
check("person stored even with no public email", len(c2) == 1 and c2[0]["name"] == "Silent Sam",
      str([dict(c) for c in c2]))
check("no email attached when none is public", not c2[0]["email"], str(c2[0]["email"]))
check("email-less contact is never mailable", dmm.mailable_contacts(lid2) == [])

# --- 5. deduplication: same person twice (and across runs) -> one row ---
cid4 = make_campaign(uid, "DM4")
lid4 = make_lead(cid4, uid)


def fake_gemini_dup(prompt):
    return ([{"name": "Anna Berg", "title": "Owner",
              "source_urls": ["https://fitlife.example.com/impressum"]},
             {"name": "anna berg", "title": "Inhaberin",
              "source_urls": ["https://fitlife.example.com/team"]}], [])


def fake_fetch_dup(url):
    if "impressum" in url or "team" in url:
        return "<html><body><p>Inhaber: Anna Berg</p></body></html>"
    return SITE_HTML


with mock.patch.object(dmm, "_gemini_call", side_effect=fake_gemini_dup), \
     mock.patch.object(dmm, "_fetch_page", side_effect=fake_fetch_dup):
    for _ in range(2):  # run twice: duplicates must not accumulate
        dmm.enrich_lead(db.q("SELECT * FROM leads WHERE id=?", (lid4,), one=True),
                        db.q("SELECT * FROM campaigns WHERE id=?", (cid4,), one=True),
                        max_contacts=4)
c4 = dmm.get_contacts(lid4)
check("contact deduplication (one row per person)", len(c4) == 1, str(len(c4)))

# --- 6. no API key: graceful pending, never blocks ---
cid5 = make_campaign(uid, "DM5")
lid5 = make_lead(cid5, uid)
with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
    import config
    old_key = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = ""
    try:
        status5, _ = dmm.enrich_lead(
            db.q("SELECT * FROM leads WHERE id=?", (lid5,), one=True),
            db.q("SELECT * FROM campaigns WHERE id=?", (cid5,), one=True),
            max_contacts=3)
    finally:
        config.GEMINI_API_KEY = old_key
check("no-key marks lead pending", status5 == "pending", str(status5))
check("no-key stores no contacts", dmm.get_contacts(lid5) == [])
check("no-key dm_status pending", db.q("SELECT dm_status FROM leads WHERE id=?",
                                       (lid5,), one=True)["dm_status"] == "pending")

# --- 7. contact email validation verdicts ---
cid6 = make_campaign(uid, "DM6")
lid6 = make_lead(cid6, uid)
now = time.time()
ct6 = db.w("INSERT INTO contacts (lead_id, name, title, email, source_url, verified, created_at) "
           "VALUES (?,?,?,?,?,?,?)",
           (lid6, "Anna Berg", "Owner", "anna@fitlife.example.com",
            "https://fitlife.example.com/impressum", 1, now))


def fake_validate(emails, max_workers=5):
    return [{"verdict": "valid", "reason": "mx ok", "mx_host": "mx.example.com"}
            for _ in emails]


with mock.patch("email_validator.validate_emails", side_effect=fake_validate):
    pipelinemod._validate(dict(db.q("SELECT * FROM campaigns WHERE id=?", (cid6,), one=True)))
c6 = db.q("SELECT * FROM contacts WHERE id=?", (ct6,), one=True)
check("contact email verdict saved", c6["email_verdict"] == "valid", str(c6["email_verdict"]))


def fake_validate_invalid(emails, max_workers=5):
    return [{"verdict": "invalid", "reason": "no mx", "mx_host": ""} for _ in emails]


ct6b = db.w("INSERT INTO contacts (lead_id, name, title, email, source_url, verified, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (lid6, "Bad Mail", "Manager", "bad@dead.example.com",
             "https://fitlife.example.com/impressum", 1, now))
with mock.patch("email_validator.validate_emails", side_effect=fake_validate_invalid):
    pipelinemod._validate(dict(db.q("SELECT * FROM campaigns WHERE id=?", (cid6,), one=True)))
c6b = db.q("SELECT * FROM contacts WHERE id=?", (ct6b,), one=True)
check("invalid contact email cleared", not c6b["email"] and c6b["email_verdict"] == "invalid",
      str(dict(c6b)))
check("invalid contact never mailable",
      all(c["id"] != ct6b for c in dmm.mailable_contacts(lid6)))

# --- 8. per-contact queueing + same-domain spread ---
cid7 = make_campaign(uid, "DM7")
lid7 = db.w("INSERT INTO leads (user_id, campaign_id, business_name, website, email, email_verdict, "
            "selected, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, cid7, "Spread Gym", "https://spread.example.com",
             "info@spread.example.com", "valid", 1, now))
for nm, em in [("Anna Berg", "anna@spread.example.com"), ("Ben Cole", "ben@spread.example.com")]:
    db.w("INSERT INTO contacts (lead_id, name, title, email, email_verdict, source_url, verified, "
         "created_at) VALUES (?,?,?,?,?,?,?,?)",
         (lid7, nm, "Owner", em, "valid", "https://spread.example.com/impressum", 1, now))
nq = queue_worker.enqueue_campaign(uid, cid7)
check("one queue row per mailable contact", nq == 2, str(nq))
camp7 = db.q("SELECT * FROM campaigns WHERE id=?", (cid7,), one=True)
out = queue_worker._process_campaign(camp7, time.time(),
                                     send_fn=lambda a, t, s, b: {"ok": True, "message_id": "x"})
ok_sends = [o for o in out if o.get("ok")]
check("same-domain contacts spread: 1 per run", len(ok_sends) == 1, str(len(ok_sends)))
left = db.q("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=? AND status='pending'",
            (cid7,), one=True)["c"]
check("deferred contact stays pending for next tick", left >= 1, str(left))
out2 = queue_worker._process_campaign(db.q("SELECT * FROM campaigns WHERE id=?", (cid7,), one=True),
                                      time.time(),
                                      send_fn=lambda a, t, s, b: {"ok": True, "message_id": "x"})
ok2 = [o for o in out2 if o.get("ok")]
check("second tick sends the deferred contact", len(ok2) == 1, str(len(ok2)))
logged = db.q("SELECT COUNT(*) c FROM send_log WHERE campaign_id=? AND contact_id IS NOT NULL",
              (cid7,), one=True)["c"]
check("send_log records contact_id", logged == 2, str(logged))

# --- 9. follow-ups stay with the same contact ---
cid8 = make_campaign(uid, "DM8")
db.w("UPDATE campaigns SET followups_enabled=1, followup_mode='fixed', followup_count=2 WHERE id=?",
     (cid8,))
import followups as followupsmod
followupsmod.upsert_followup(cid8, 1, "Re: Hi {business_name}", "Bumping this, {contact_name}.")
followupsmod.upsert_followup(cid8, 2, "Re: Hi {business_name}", "Last bump.")
lid8 = db.w("INSERT INTO leads (user_id, campaign_id, business_name, website, email, email_verdict, "
            "selected, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, cid8, "Chain Gym", "https://chain.example.com",
             "info@chain.example.com", "valid", 1, now))
ct8 = db.w("INSERT INTO contacts (lead_id, name, title, email, email_verdict, source_url, verified, "
           "created_at) VALUES (?,?,?,?,?,?,?,?)",
           (lid8, "Anna Berg", "Owner", "anna@chain.example.com", "valid",
            "https://chain.example.com/impressum", 1, now))
queue_worker.enqueue_campaign(uid, cid8)
camp8 = db.q("SELECT * FROM campaigns WHERE id=?", (cid8,), one=True)
queue_worker._process_campaign(camp8, time.time(),
                               send_fn=lambda a, t, s, b: {"ok": True, "message_id": "x"})
fu = db.q("SELECT * FROM send_queue WHERE campaign_id=? AND step=1", (cid8,), one=True)
check("follow-up queued for the same contact", fu is not None and fu["contact_id"] == ct8,
      str(dict(fu) if fu else None))
allowed, _ = sender.sequence_gate(uid, cid8,
                                  db.q("SELECT * FROM leads WHERE id=?", (lid8,), one=True),
                                  1, contact_id=ct8)
check("sequence_gate allows contact follow-up after contact step-0", allowed)
allowed2, _ = sender.sequence_gate(uid, cid8,
                                   db.q("SELECT * FROM leads WHERE id=?", (lid8,), one=True),
                                   1, contact_id=999999)
check("sequence_gate is per contact (other contact blocked)", not allowed2)

# --- 10. reply from a contact email stops the lead ---
cid9 = make_campaign(uid, "DM9")
lid9 = db.w("INSERT INTO leads (user_id, campaign_id, business_name, website, email, email_verdict, "
            "selected, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, cid9, "Reply Gym", "https://reply.example.com",
             "info@reply.example.com", "valid", 1, now))
db.w("INSERT INTO contacts (lead_id, name, title, email, email_verdict, source_url, verified, "
     "created_at) VALUES (?,?,?,?,?,?,?,?)",
     (lid9, "Anna Berg", "Owner", "anna@reply.example.com", "valid",
      "https://reply.example.com/impressum", 1, now))
sender.mark_replies(uid, [{"address": "anna@reply.example.com",
                          "snippet": "thanks, interested", "date": None}])
replied = db.q("SELECT replied FROM leads WHERE id=?", (lid9,), one=True)["replied"]
check("reply from contact email stops the lead", bool(replied))

# --- 11. dm toggle off: people stage skips to validate, nothing stored ---
cid10 = make_campaign(uid, "DM10")
db.w("UPDATE campaigns SET dm_enabled=0 WHERE id=?", (cid10,))
lid10 = make_lead(cid10, uid)
nxt10 = pipelinemod._people(dict(db.q("SELECT * FROM campaigns WHERE id=?", (cid10,), one=True)))
check("dm toggle off skips enrichment", nxt10 == "validate")
check("no contacts stored when disabled", dmm.get_contacts(lid10) == [])

# --- 12. stats include dm counts ---
cid11 = make_campaign(uid, "DM11")
lid11 = make_lead(cid11, uid)
db.w("INSERT INTO contacts (lead_id, name, title, email, email_verdict, source_url, verified, "
     "created_at) VALUES (?,?,?,?,?,?,?,?)",
     (lid11, "Anna Berg", "Owner", "anna@dm11.example.com", "valid",
      "https://dm11.example.com/impressum", 1, now))
st = pipelinemod.stats(cid11)
check("stats report dm leads", st.get("dm_leads") == 1, str(st))
check("stats report dm total", st.get("dm_total") == 1, str(st))
check("lead_dm_counts per lead", dmm.lead_dm_counts(cid11).get(lid11) == 1)

# --- 13. migration safety on a legacy-style fresh database ---
import sqlite3
LEG = "/tmp/dmlegacy.db"
if os.path.exists(LEG):
    os.remove(LEG)
con = sqlite3.connect(LEG)
con.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, campaign_id INTEGER)")
con.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY)")
con.execute("CREATE TABLE send_queue (id INTEGER PRIMARY KEY)")
con.execute("CREATE TABLE send_log (id INTEGER PRIMARY KEY)")
con.commit()
con.close()
os.environ["DATABASE_URL"] = "sqlite:///" + LEG
import importlib
importlib.reload(db)
try:
    db.init_db()
    ok_mig = True
except Exception as e:
    ok_mig = False
    print("migration error:", e)
check("legacy tables migrate without errors", ok_mig)
tables = [r["name"] for r in db.q(
    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('contacts')")]
check("contacts table created by migration", tables == ["contacts"])
cols = [r["name"] for r in db.q("PRAGMA table_info(campaigns)")]
check("campaign dm columns added by migration",
      "dm_enabled" in cols and "dm_max_contacts" in cols, str(cols))
cols = [r["name"] for r in db.q("PRAGMA table_info(leads)")]
check("lead dm_status added by migration", "dm_status" in cols, str(cols))
cols = [r["name"] for r in db.q("PRAGMA table_info(send_queue)")]
check("send_queue contact_id added by migration", "contact_id" in cols, str(cols))
cols = [r["name"] for r in db.q("PRAGMA table_info(send_log)")]
check("send_log contact_id added by migration", "contact_id" in cols, str(cols))

print()
print(f"{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
