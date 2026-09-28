"""Auth-focused tests: duplicate-email signup block, legacy-duplicate login
resolution, and the forgot-password flow.

Self-contained (own SQLite DB at /tmp/v2test_auth.db) so it can run
alongside tests/test_all.py without interference.

Run: ~/workspace/venvs/shakedown-venv/bin/python tests/test_auth.py
"""
import hashlib
import os
import sys
import time
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DB_PATH = "/tmp/v2test_auth.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH
os.environ["FERNET_KEY"] = "6V9xJvJxQJ0QZk8mZQp3vQw9zY8xK7vJ6uQ5tR4sS3qP2oO1nN0mM9lL8kK7=="
os.environ["CRON_SECRET"] = "test-cron-secret-123"
os.environ["APP_URL"] = "http://localhost:5000"

from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash

import db
import auth as authmod
import crypto as cryptomod
import app as appmod

db.init_db()

# Simulate a LEGACY database (like production today): rebuild app_users
# WITHOUT the unique constraint, so duplicate rows can exist.
from sqlalchemy import text as _text
with db.engine.begin() as _con:
    _con.execute(_text("ALTER TABLE app_users RENAME TO _app_users_new"))
    _con.execute(_text("CREATE TABLE app_users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "email TEXT NOT NULL, name TEXT DEFAULT '', "
                       "password_hash TEXT NOT NULL, created_at FLOAT NOT NULL)"))
    _con.execute(_text("INSERT INTO app_users (id, email, name, password_hash, created_at) "
                       "SELECT id, email, name, password_hash, created_at FROM _app_users_new"))
    _con.execute(_text("DROP TABLE _app_users_new"))

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def raw_user(email, pw, name="Raw"):
    return db.w("INSERT INTO app_users (email, name, password_hash, created_at) "
                "VALUES (?,?,?,?)",
                (email, name, generate_password_hash(pw), time.time()))


def add_sender_account(user_id, email="sender@example.com"):
    return db.w("""INSERT INTO sender_accounts
        (user_id, email, password_enc, daily_cap, warmup_enabled, warmup_start,
         status, sent_today, sent_date, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (user_id, email, cryptomod.encrypt_token("abcdefghijklmnop"),
         30, 0, time.time(), "active", 0, "", time.time()))


def add_campaign(user_id, name="Camp"):
    return db.w("""INSERT INTO campaigns
        (user_id, name, niche, location, status, dry_run, created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (user_id, name, "gyms", "Berlin", "active", 1, time.time()))


# ---------- 1. duplicate signup block ----------
ok, u = authmod.register_user("dup1@example.com", "A", "password123")
check("register first account ok", ok)
ok, err = authmod.register_user("dup1@example.com", "B", "password123")
check("duplicate email rejected", not ok and err == "An account with this email already exists. Please log in.", repr(err))
ok, ucase = authmod.register_user("Case@Example.com", "C", "password123")
check("mixed-case register ok", ok)
ok, err = authmod.register_user("case@example.com", "D", "password123")
check("case-insensitive duplicate rejected", not ok and "already exists" in err, repr(err))
ok, err = authmod.register_user("  DUP1@EXAMPLE.COM  ", "E", "password123")
check("whitespace/case duplicate rejected", not ok and "already exists" in err, repr(err))

# ---------- 2. legacy duplicates: login resolves to data-bearing account ----------
old_id = raw_user("legacy@example.com", "oldpassword123", "Old")
new_id = raw_user("legacy@example.com", "newpassword123", "New")
check("legacy dupes created", old_id != new_id)
add_campaign(old_id)  # data lives on the ORIGINAL account
u = authmod.verify_login("legacy@example.com", "oldpassword123")
check("login lands in data-bearing (original) account", u is not None and u["id"] == old_id, repr(u and u["id"]))
u = authmod.verify_login("legacy@example.com", "newpassword123")
check("empty duplicate password still reaches its own account", u is not None and u["id"] == new_id, repr(u and u["id"]))
check("wrong password rejected across dupes", authmod.verify_login("legacy@example.com", "nope") is None)
# data on the NEWER account instead -> newer wins
old2 = raw_user("legacy2@example.com", "oldpw12345", "Old2")
new2 = raw_user("legacy2@example.com", "newpw12345", "New2")
add_campaign(new2)
u = authmod.verify_login("legacy2@example.com", "newpw12345")
check("login prefers data-bearing newer account", u is not None and u["id"] == new2, repr(u and u["id"]))
u = authmod.verify_login("legacy2@example.com", "oldpw12345")
check("original password still works when it has no data", u is not None and u["id"] == old2, repr(u and u["id"]))

# ---------- 3. forgot-password flow ----------
ok, ru = authmod.register_user("resetme@example.com", "Resetty", "password123")
ruid = ru["id"]
add_sender_account(ruid)
sent = []
with mock.patch("smtp_mail.send_message", side_effect=lambda *a, **k: sent.append(a) or True):
    ok, msg = authmod.request_password_reset("resetme@example.com")
check("reset request ok + generic message", ok and msg == authmod.RESET_GENERIC_MESSAGE, repr(msg))
check("reset email sent to user", len(sent) == 1 and sent[0][1] == "resetme@example.com", repr(sent))
check("reset link in email body", len(sent) == 1 and "/reset-password/" in sent[0][3], "")
row = db.q("SELECT * FROM password_resets WHERE user_id=?", (ruid,), one=True)
check("token row stored hashed, not raw", row is not None and len(row["token_hash"]) == 64
      and row["used_at"] is None, repr(row))
# extract token from the email body
import re
m = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", sent[0][3])
token = m.group(1)
ok, msg = authmod.redeem_password_reset(token, "brandnewpw1")
check("redeem sets new password", ok, repr(msg))
check("login works with new password", authmod.verify_login("resetme@example.com", "brandnewpw1") is not None)
check("old password dead", authmod.verify_login("resetme@example.com", "password123") is None)
ok, msg = authmod.redeem_password_reset(token, "anotherpw12")
check("reused token rejected", not ok and "already been used" in msg, repr(msg))
ok, msg = authmod.redeem_password_reset("bogus-token", "anotherpw12")
check("bogus token rejected", not ok and "invalid" in msg, repr(msg))
ok, msg = authmod.redeem_password_reset(token, "short")
check("short new password rejected", not ok and "at least" in msg, repr(msg))

# unknown email -> generic message, no token row
before = db.q("SELECT COUNT(*) c FROM password_resets", one=True)["c"]
ok, msg = authmod.request_password_reset("nobody-here@example.com")
after = db.q("SELECT COUNT(*) c FROM password_resets", one=True)["c"]
check("unknown email gets generic message, no token", ok and msg == authmod.RESET_GENERIC_MESSAGE and before == after)

# expired token
tok_exp = "expiredtok123"
db.w("INSERT INTO password_resets (user_id, email, token_hash, created_at, expires_at, used_at)"
     " VALUES (?,?,?,?,?,NULL)",
     (ruid, "resetme@example.com", hashlib.sha256(tok_exp.encode()).hexdigest(),
      time.time() - 7200, time.time() - 3600))
ok, msg = authmod.redeem_password_reset(tok_exp, "validpass12")
check("expired token rejected", not ok and "expired" in msg, repr(msg))

# rate limit: max 3 per hour
ok, rlu = authmod.register_user("ratelimit@example.com", "RL", "password123")
add_sender_account(rlu["id"])
with mock.patch("smtp_mail.send_message", return_value=True):
    for _ in range(3):
        authmod.request_password_reset("ratelimit@example.com")
    n3 = db.q("SELECT COUNT(*) c FROM password_resets WHERE email=?", ("ratelimit@example.com",), one=True)["c"]
    ok, msg = authmod.request_password_reset("ratelimit@example.com")
    n4 = db.q("SELECT COUNT(*) c FROM password_resets WHERE email=?", ("ratelimit@example.com",), one=True)["c"]
check("rate limit caps at 3/hour (still generic message)", n3 == 3 and n4 == 3 and ok and msg == authmod.RESET_GENERIC_MESSAGE,
      f"n3={n3} n4={n4}")

# no mail sender available -> clear error
ok, nmu = authmod.register_user("nomail@example.com", "NM", "password123")
ok, msg = authmod.request_password_reset("nomail@example.com")
check("no sender account -> clear mail error", not ok and "mail sender" in msg, repr(msg))

# ---------- 4. routes ----------
client = appmod.app.test_client()
r = client.get("/forgot-password")
check("forgot-password page renders", r.status_code == 200 and b"Reset your password" in r.data)
with mock.patch("smtp_mail.send_message", return_value=True):
    r = client.post("/forgot-password", data={"email": "resetme@example.com"})
check("forgot-password POST generic message", r.status_code == 200 and b"If an account exists" in r.data)
r = client.post("/forgot-password", data={"email": "nomail@example.com"})
check("forgot-password POST mail error shown", r.status_code == 200 and b"mail sender" in r.data)
r = client.get("/reset-password/some-token")
check("reset-password page renders", r.status_code == 200 and b"Choose a new password" in r.data)
r = client.get("/login")
check("login page has forgot-password link", r.status_code == 200 and b"/forgot-password" in r.data)
# full route round-trip: request -> redeem via posted token
with mock.patch("smtp_mail.send_message", side_effect=lambda *a, **k: sent.append(a) or True):
    authmod.request_password_reset("resetme@example.com")
m2 = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", sent[-1][3])
r = client.post(f"/reset-password/{m2.group(1)}", data={"password": "viapageroute1"})
check("reset-password POST completes", r.status_code == 200 and b"Password updated" in r.data)
check("route-set password logs in", authmod.verify_login("resetme@example.com", "viapageroute1") is not None)

# ---------- 5. unique-constraint migration ----------
added = db._ensure_app_users_email_unique()
check("unique migration skipped while dupes exist", added is False)
# (legacy dupes exist in this db, so the index must NOT be present)
import sqlite3
con = sqlite3.connect(DB_PATH)
idxs = [dict(ix) for ix in con.execute("PRAGMA index_list(app_users)").fetchall()]
has_uidx = any(ix["name"] == "app_users_email_uidx" for ix in idxs)
check("no unique index while legacy dupes present", not has_uidx)
con.close()
# resolve the legacy dupes (as the user would: keep the data-bearing account)
db.w("DELETE FROM app_users WHERE id=?", (new_id,))
db.w("DELETE FROM app_users WHERE id=?", (old2,))
added = db._ensure_app_users_email_unique()
check("migration adds constraint once dupes resolved", added is True)
try:
    raw_user("dup1@example.com", "whatever12345", "Sneaky")
    db_blocked = False
except Exception:
    db_blocked = True
check("DB-level duplicate insert blocked after migration", db_blocked)
check("migration idempotent on second run", db._ensure_app_users_email_unique() is False)
ok, err = authmod.register_user("dup1@example.com", "Z", "password123")
check("app-level duplicate still rejected", not ok and "already exists" in err, repr(err))
# login still resolves to the surviving data-bearing accounts
u = authmod.verify_login("legacy@example.com", "oldpassword123")
check("post-cleanup login hits original account", u is not None and u["id"] == old_id)
u = authmod.verify_login("legacy2@example.com", "newpw12345")
check("post-cleanup login hits data-bearing account", u is not None and u["id"] == new2)

print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
