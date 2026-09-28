"""Social links + daily CSV export tests.

Self-contained (own SQLite DB at /tmp/v2test_social.db) so it can run
alongside the other suites without interference.

Run: ~/workspace/venvs/shakedown-venv/bin/python tests/test_social_csv.py
"""
import csv
import io
import os
import sys
import time
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DB_PATH = "/tmp/v2test_social.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["CRON_SECRET"] = "test-cron-secret-123"

from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

import db
import enrich as enrichmod
import auth as authmod
import app as appmod
import decision_makers as dmm

db.init_db()

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


# ---------- 1. extractor: finds self-published links ----------
HTML_GOOD = """
<html><body><footer>
<a href="https://www.linkedin.com/company/acme-plumbing-co/">LinkedIn</a>
<a href="https://instagram.com/acme.plumbing">IG</a>
<a href="https://www.facebook.com/AcmePlumbing">FB</a>
<a href="https://x.com/acmeplumbing">X</a>
</footer></body></html>"""
soc = enrichmod._extract_social_urls(HTML_GOOD)
check("finds linkedin company page",
      soc["linkedin_url"] == "https://www.linkedin.com/company/acme-plumbing-co", repr(soc))
check("finds instagram profile",
      soc["instagram_url"] == "https://instagram.com/acme.plumbing", repr(soc))
check("finds facebook page",
      soc["facebook_url"] == "https://www.facebook.com/AcmePlumbing", repr(soc))
check("finds x profile",
      soc["x_url"] == "https://x.com/acmeplumbing", repr(soc))

# ---------- 2. extractor: rejects share widgets / personal / junk ----------
HTML_JUNK = """
<html><body>
<a href="https://www.facebook.com/sharer/sharer.php?u=http://example.com">share</a>
<a href="https://www.linkedin.com/shareArticle?mini=true&url=x">share</a>
<a href="https://twitter.com/intent/tweet?text=hi">tweet</a>
<a href="https://www.instagram.com/p/Cx123abc/">post</a>
<a href="https://www.linkedin.com/in/john-doe-123">personal</a>
<a href="https://www.facebook.com/dialog/share">dialog</a>
<a href="https://x.com/i/flow/login">login</a>
</body></html>"""
soc2 = enrichmod._extract_social_urls(HTML_JUNK)
check("rejects all share-widget / personal / junk links",
      all(v == "" for v in soc2.values()), repr(soc2))

# ---------- 3. normalization ----------
HTML_NORM = '<a href="https://instagram.com/acme/?utm_source=x#frag">ig</a>'
soc3 = enrichmod._extract_social_urls(HTML_NORM)
check("strips query/fragment/trailing slash",
      soc3["instagram_url"] == "https://instagram.com/acme", repr(soc3))

# ---------- 4. empty / no html ----------
soc4 = enrichmod._extract_social_urls("")
check("empty html -> all blank", all(v == "" for v in soc4.values()))

# ---------- 5. enrich_website exposes social key (mocked fetch) ----------
fake = mock.Mock(status_code=200, headers={"Content-Type": "text/html"},
                 text="<html><head><title>Acme</title></head><body>"
                      '<a href="https://linkedin.com/company/acme/">in</a></body></html>')
with mock.patch("enrich.requests.get", return_value=fake):
    res = enrichmod.enrich_website("acme.example.com", pause=0)
check("enrich_website returns social dict",
      res["social"]["linkedin_url"] == "https://linkedin.com/company/acme", repr(res["social"]))

# ---------- 6. migration columns exist ----------
cols = [r["name"] for r in db.q("PRAGMA table_info(leads)")]
check("social columns migrated",
      all(c in cols for c in ("linkedin_url", "instagram_url", "facebook_url", "x_url")),
      repr(cols))

# ---------- 7. save_social persists ----------
ok, user = authmod.register_user("social@example.com", "Tester", "password123")
assert ok, user
UID = user["id"]
CID = db.w("INSERT INTO campaigns (user_id, name, niche, location, created_at) VALUES (?,?,?,?,?)",
           (UID, "Social CSV Test", "plumber", "Austin TX", time.time()))
LID = db.w("INSERT INTO leads (user_id, campaign_id, business_name, website, created_at) VALUES (?,?,?,?,?)",
           (UID, CID, "Acme Plumbing", "https://acme.example.com", time.time()))
enrichmod.save_social(LID, {"social": {"linkedin_url": "https://linkedin.com/company/acme",
                                       "instagram_url": "https://instagram.com/acme",
                                       "facebook_url": "", "x_url": ""}})
lead = db.q("SELECT * FROM leads WHERE id=?", (LID,), one=True)
check("save_social persists urls",
      lead["linkedin_url"] == "https://linkedin.com/company/acme"
      and lead["instagram_url"] == "https://instagram.com/acme"
      and lead["facebook_url"] == "", repr({k: lead[k] for k in ("linkedin_url", "instagram_url")}))
# no-op when nothing found
enrichmod.save_social(LID, {"social": {"linkedin_url": "", "instagram_url": "",
                                       "facebook_url": "", "x_url": ""}})
check("save_social no-op is safe", True)

# ---------- 8. daily CSV route ----------
YESTERDAY = time.time() - 90000
LID_OLD = db.w("INSERT INTO leads (user_id, campaign_id, business_name, website, address, created_at) "
               "VALUES (?,?,?,?,?,?)",
               (UID, CID, "Old Shop", "https://old.example.com", "Old Town", YESTERDAY))
db.w("UPDATE leads SET address=?, email=?, email_verdict=? WHERE id=?",
     ("Austin TX", "info@acme.example.com", "valid", LID))
# verified + unverified decision-maker contacts
db.w("INSERT INTO contacts (lead_id, name, title, email, email_verdict, source_url, verified, created_at) "
     "VALUES (?,?,?,?,?,?,?,?)",
     (LID, "Jane Doe", "Owner", "", "", "https://acme.example.com/imprint", 1, time.time()))
db.w("INSERT INTO contacts (lead_id, name, title, email, email_verdict, source_url, verified, created_at) "
     "VALUES (?,?,?,?,?,?,?,?)",
     (LID, "Ghost Person", "Manager", "", "", "https://random.example.com", 0, time.time()))

client = appmod.app.test_client()
r = client.post("/login", data={"email": "social@example.com", "password": "password123"})
check("test login works", r.status_code in (302, 303), r.status_code)
r = client.get(f"/campaign/{CID}/export/daily")
check("export route 200", r.status_code == 200, r.status_code)
check("export content-type csv", "text/csv" in r.headers.get("Content-Type", ""),
      r.headers.get("Content-Type"))
check("export content-disposition attachment",
      "attachment" in r.headers.get("Content-Disposition", "") and r.headers.get("Content-Disposition", "").endswith(".csv\""),
      r.headers.get("Content-Disposition"))
rows = list(csv.reader(io.StringIO(r.data.decode("utf-8"))))
check("csv header row correct",
      rows[0] == ["business_name", "website", "city/address", "discovery_date",
                  "linkedin_url", "instagram_url", "facebook_url",
                  "decision_makers", "email", "email_verdict"], repr(rows[0]))
data_rows = rows[1:]
names = [row[0] for row in data_rows]
check("only today's leads exported", "Acme Plumbing" in names and "Old Shop" not in names, repr(names))
acme = [row for row in data_rows if row[0] == "Acme Plumbing"][0]
check("csv includes social url", acme[4] == "https://linkedin.com/company/acme", repr(acme))
check("csv includes verified DM, excludes unverified",
      "Jane Doe, Owner" in acme[7] and "Ghost Person" not in acme[7], repr(acme[7]))
check("csv includes email + verdict",
      acme[8] == "info@acme.example.com" and acme[9] == "valid", repr(acme[8:]))

# ---------- 9. campaign page: export button + social links ----------
r = client.get(f"/campaign/{CID}")
check("campaign page 200", r.status_code == 200, r.status_code)
check("campaign page has Export today's CSV button",
      b"Export today" in r.data and f"/campaign/{CID}/export/daily".encode() in r.data)
check("campaign page shows social link",
      b"https://linkedin.com/company/acme" in r.data)

# ---------- 10. other user's campaign -> 404 ----------
ok, user2 = authmod.register_user("social2@example.com", "Other", "password123")
assert ok
client2 = appmod.app.test_client()
client2.post("/login", data={"email": "social2@example.com", "password": "password123"})
r = client2.get(f"/campaign/{CID}/export/daily")
check("export of another user's campaign -> 404", r.status_code == 404, r.status_code)

# ---------- 11. unauthenticated -> redirect to login ----------
client3 = appmod.app.test_client()
r = client3.get(f"/campaign/{CID}/export/daily")
check("unauthenticated export redirects", r.status_code in (301, 302, 303), r.status_code)

print(f"\n{len(passed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
