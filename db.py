"""Database layer: Postgres (Neon) via SQLAlchemy when DATABASE_URL is set,
local SQLite fallback for development.

Keeps the same tiny interface as the original local app so the ported
modules (sender, leads, jobs, queue_worker) barely change:

    q(sql, args=(), one=False)  -> list of dicts (or single dict / None)
    w(sql, args=())             -> last inserted row id (for INSERTs)
    init_db()                   -> create all tables if missing

Write SQL with `?` placeholders; they are translated to `%s` on Postgres.

V2 schema note: this version uses built-in email+password auth and Gmail
SMTP via App Passwords. The tables `app_users` and `sender_accounts` are
new. If this database was previously used by the Google-OAuth version, its
old `users` (google_sub) and `gmail_accounts` (OAuth tokens) tables are
left untouched and ignored - create_all() only adds tables that do not
exist yet (CREATE TABLE IF NOT EXISTS), and no data is ever deleted.
"""
import os
import time

from sqlalchemy import (create_engine, MetaData, Table, Column, Integer,
                        Text, Float, text as _sd)

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if config.DATABASE_URL:
    _url = config.DATABASE_URL
    # SQLAlchemy needs the postgresql+psycopg2 scheme; Neon URLs are fine as-is.
    if _url.startswith("postgres://"):
        _url = _url.replace("postgres://", "postgresql+psycopg2://", 1)
        IS_POSTGRES = True
    elif _url.startswith("postgresql://"):
        _url = _url.replace("postgresql://", "postgresql+psycopg2://", 1)
        IS_POSTGRES = True
    else:
        IS_POSTGRES = False  # e.g. sqlite:///... in tests
    engine = create_engine(_url, pool_pre_ping=True,
                           **({"pool_size": 3, "max_overflow": 5} if IS_POSTGRES else
                              {"connect_args": {"check_same_thread": False}}))
else:
    IS_POSTGRES = False
    _sqlite_path = os.path.join(BASE_DIR, "outreach.db")
    # On serverless (Vercel) the filesystem is read-only except /tmp; SQLite
    # there is only ever a dev fallback, never production storage.
    if os.environ.get("VERCEL"):
        _sqlite_path = "/tmp/outreach.db"
    engine = create_engine(f"sqlite:///{_sqlite_path}",
                           connect_args={"check_same_thread": False})

metadata = MetaData()

# V2 users: built-in email + password auth. (The old google-based `users`
# table, if present from the previous version, is ignored.)
app_users = Table("app_users", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("email", Text, unique=True, nullable=False),
                  Column("name", Text, default=""),
                  Column("password_hash", Text, nullable=False),
                  Column("created_at", Float, nullable=False))

# V2 sender accounts: Gmail address + Fernet-encrypted App Password for
# direct SMTP sending (smtp.gmail.com:587) and IMAP bounce scans.
# (The old OAuth-based `gmail_accounts` table, if present, is ignored.)
sender_accounts = Table("sender_accounts", metadata,
                        Column("id", Integer, primary_key=True, autoincrement=True),
                        Column("user_id", Integer, nullable=False),
                        Column("email", Text, nullable=False),
                        Column("password_enc", Text, nullable=False),
                        Column("daily_cap", Integer, nullable=False, server_default="30"),
                        Column("warmup_enabled", Integer, nullable=False, server_default="1"),
                        Column("warmup_start", Float, nullable=False),
                        Column("status", Text, nullable=False, server_default=_sd("'active'")),
                        Column("sent_today", Integer, nullable=False, server_default="0"),
                        Column("sent_date", Text, nullable=False, server_default=_sd("''")),
                        Column("last_account_note", Text, default=""),
                        Column("created_at", Float, nullable=False))

# Password-reset tokens: single-use, 1-hour expiry, stored hashed.
password_resets = Table("password_resets", metadata,
                        Column("id", Integer, primary_key=True, autoincrement=True),
                        Column("user_id", Integer, nullable=False),
                        Column("email", Text, nullable=False, default=""),
                        Column("token_hash", Text, nullable=False, unique=True),
                        Column("created_at", Float, nullable=False),
                        Column("expires_at", Float, nullable=False),
                        Column("used_at", Float, nullable=True))

campaigns = Table("campaigns", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("user_id", Integer, nullable=False),
                  Column("name", Text, nullable=False),
                  Column("niche", Text, default=""),
                  Column("location", Text, default=""),
                  Column("icp_notes", Text, default=""),
                  Column("subject_tpl", Text, default=""),
                  Column("body_tpl", Text, default=""),
                  Column("delay_min", Integer, nullable=False, server_default="60"),
                  Column("delay_max", Integer, nullable=False, server_default="180"),
                  Column("window_start", Text, nullable=False, server_default=_sd("'09:00'")),
                  Column("window_end", Text, nullable=False, server_default=_sd("'17:00'")),
                  Column("dry_run", Integer, nullable=False, server_default="1"),
                  Column("followups_enabled", Integer, nullable=False, server_default="1"),
                  Column("followup_count", Integer, nullable=False, server_default="5"),
                  Column("followup_delay_hours", Integer, nullable=False, server_default="40"),
                  Column("status", Text, nullable=False, server_default=_sd("'draft'")),
                  Column("next_send_at", Float),  # nullable; worker treats None as 0
                  Column("last_account_id", Integer, default=0),
                  # Full Autopilot pipeline: niche + location in, everything
                  # else automatic. stage is one of discover/enrich/validate/
                  # write/queue/done; cursor holds small JSON resume state.
                  Column("pipeline_enabled", Integer, nullable=False, server_default="0"),
                  Column("pipeline_target_leads", Integer, nullable=False, server_default="50"),
                  # Daily discovery throttle for the continuous growth loop:
                  # discovery pauses each day after this many NEW leads.
                  Column("daily_discovery_target", Integer, nullable=False, server_default="100"),
                  Column("pipeline_stage", Text, default=""),
                  Column("pipeline_cursor", Text, default=""),
                  # Decision-maker enrichment (decision_makers.py): find real,
                  # verified people per business and mail them individually.
                  # dm_enabled=0 skips the "people" pipeline stage entirely;
                  # dm_max_contacts caps stored+mailed contacts per business.
                  Column("dm_enabled", Integer, nullable=False, server_default="1"),
                  Column("dm_max_contacts", Integer, nullable=False, server_default="3"),
                  Column("created_at", Float, nullable=False))

leads = Table("leads", metadata,
              Column("id", Integer, primary_key=True, autoincrement=True),
              Column("user_id", Integer, nullable=False),
              Column("campaign_id", Integer),
              Column("business_name", Text, default=""),
              Column("address", Text, default=""),
              Column("phone", Text, default=""),
              Column("website", Text, default=""),
              Column("email", Text, default=""),
              Column("email_verdict", Text, default=""),
              Column("email_verdict_detail", Text, default=""),
              Column("has_contact_form", Integer, nullable=False, server_default="0"),
              Column("rating", Text, default=""),
              Column("review_count", Text, default=""),
              Column("category", Text, default=""),
              Column("source", Text, default="csv"),
              Column("notes", Text, default=""),
              Column("selected", Integer, nullable=False, server_default="1"),
              Column("replied", Integer, nullable=False, server_default="0"),
              # replied=1: lead replied (via IMAP reply scan); sequence stops
              Column("handled", Integer, nullable=False, server_default="0"),
              # handled=1: user marked the replied lead handled; hidden from the hot list
              Column("unsubscribed", Integer, nullable=False, server_default="0"),
              # unsubscribed=1: lead opted out via the one-click unsubscribe
              # link; never queued or mailed again (same as reply/bounce guards)
              Column("fit", Text, default=""),
              # lead_quality.detect_fit: "possible_chain" when chain/franchise
              # signals are found, "independent" when checked with no signals,
              # "" when not checked yet. The ICP is independent businesses.
              Column("dm_status", Text, default=""),
              # decision-maker enrichment state: "" (not attempted),
              # "pending" (no AI key or transient failure; retried),
              # "done" (attempted), "skipped" (no website to search).
              Column("created_at", Float, nullable=False))

send_queue = Table("send_queue", metadata,
                   Column("id", Integer, primary_key=True, autoincrement=True),
                   Column("campaign_id", Integer, nullable=False),
                   Column("lead_id", Integer, nullable=False),
                   # Which verified decision-maker contact this row mails
                   # (contacts.id). NULL = the lead's own general email
                   # (the pre-contacts behavior, kept as the fallback).
                   Column("contact_id", Integer),
                   Column("status", Text, nullable=False, server_default=_sd("'pending'")),
                   Column("scheduled_at", Float, nullable=False, server_default="0"),
                   Column("attempts", Integer, nullable=False, server_default="0"),
                   Column("step", Integer, nullable=False, server_default="0"),
                   # step 0 = initial message, 1..10 = follow-up N
                   Column("last_error", Text, default=""))

# Follow-up sequence templates, per campaign. Steps are 1..10; the campaign
# row controls whether follow-ups are enabled, how many steps run, and the
# hours between steps. A missing/blank step ends the chain there.
followups = Table("followups", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("campaign_id", Integer, nullable=False),
                  Column("step", Integer, nullable=False),  # 1..10
                  Column("subject_tpl", Text, default=""),
                  Column("body_tpl", Text, default=""),
                  Column("created_at", Float, nullable=False))

send_log = Table("send_log", metadata,
                 Column("id", Integer, primary_key=True, autoincrement=True),
                 Column("user_id", Integer, nullable=False),
                 Column("campaign_id", Integer),
                 Column("account_id", Integer),
                 Column("lead_id", Integer),
                 # The decision-maker contact mailed (contacts.id), if any;
                 # NULL = the lead's general email.
                 Column("contact_id", Integer),
                 Column("step", Integer, nullable=False, server_default="0"),
                 # step 0 = initial message, 1..10 = follow-up N
                 Column("recipient", Text, nullable=False),
                 Column("subject_rendered", Text, default=""),
                 Column("sent_at", Float, nullable=False),
                 Column("status", Text, nullable=False),
                 Column("error", Text, default=""),
                 Column("dry_run", Integer, nullable=False, server_default="0"))

jobs = Table("jobs", metadata,
             Column("id", Integer, primary_key=True, autoincrement=True),
             Column("user_id", Integer, nullable=False),
             Column("campaign_id", Integer),
             Column("kind", Text, nullable=False),
             Column("status", Text, nullable=False, server_default=_sd("'running'")),
             Column("total", Integer, nullable=False, server_default="0"),
             Column("done", Integer, nullable=False, server_default="0"),
             Column("result", Text, default=""),
             Column("payload", Text, default=""),  # JSON cursor for chunked serverless jobs
             Column("created_at", Float, nullable=False))

# Autopilot AI content: one cached AI-written email + 3 follow-ups per lead.
# Generated once (by the autopilot job); sending only reads this table, so a
# lead is never generated twice and costs stay predictable.
ai_content = Table("ai_content", metadata,
                   Column("id", Integer, primary_key=True, autoincrement=True),
                   Column("lead_id", Integer, nullable=False, unique=True),
                   Column("subject", Text, default=""),
                   Column("body", Text, default=""),
                   Column("fu1_subj", Text, default=""),
                   Column("fu1_body", Text, default=""),
                   Column("fu2_subj", Text, default=""),
                   Column("fu2_body", Text, default=""),
                   Column("fu3_subj", Text, default=""),
                   Column("fu3_body", Text, default=""),
                   Column("findings_json", Text, default="[]"),
                   Column("tokens_in", Integer, nullable=False, server_default="0"),
                   Column("tokens_out", Integer, nullable=False, server_default="0"),
                   Column("created_at", Float, nullable=False))
# Decision-maker contacts: real people behind each business, found by the
# decision_makers module (Gemini with web-search grounding, corroborated
# against the business's own site/imprint). Accuracy rule: only verified
# people are stored; uncorroborated AI suggestions are discarded.
# verified=1 means the person was corroborated (site match or 2+
# independent sources). email is ONLY a publicly listed address tied to
# that person; '' means none found (never guessed, never mailed).
contacts = Table("contacts", metadata,
                 Column("id", Integer, primary_key=True, autoincrement=True),
                 Column("lead_id", Integer, nullable=False),
                 Column("name", Text, default=""),
                 Column("title", Text, default=""),
                 Column("email", Text, default=""),
                 Column("email_verdict", Text, default=""),
                 Column("email_verdict_detail", Text, default=""),
                 Column("source_url", Text, default=""),
                 Column("verified", Integer, nullable=False, server_default="0"),
                 Column("created_at", Float, nullable=False))
# Reply notifications: one row per replied lead per user. created_at is the
# detection time; read_at is set when the user opens the notifications page.
notifications = Table("notifications", metadata,
                      Column("id", Integer, primary_key=True, autoincrement=True),
                      Column("user_id", Integer, nullable=False),
                      Column("lead_id", Integer),
                      Column("campaign_id", Integer),
                      Column("kind", Text, nullable=False, server_default=_sd("'reply'")),
                      Column("title", Text, default=""),
                      Column("snippet", Text, default=""),
                      Column("created_at", Float, nullable=False),
                      Column("read_at", Float, nullable=True))


# Per-user compliance settings for the cold-email footer: company name and
# physical address, editable on the Settings page. Env vars COMPANY_NAME /
# COMPANY_ADDRESS in config.py act as defaults when a row/field is unset.
user_settings = Table("user_settings", metadata,
                      Column("user_id", Integer, primary_key=True),
                      Column("company_name", Text, default=""),
                      Column("company_address", Text, default=""),
                      Column("updated_at", Float, nullable=False,
                             server_default="0"))


def init_db():
    # CREATE TABLE IF NOT EXISTS under the hood; safe to run on every boot,
    # including against a database created by the previous Google-OAuth version.
    metadata.create_all(engine)
    _migrate()


def _migrate():
    """Idempotent column migrations for tables created by earlier versions.

    create_all() only adds missing TABLES, not missing columns, so new
    columns on old tables (e.g. the production Neon database, which already
    has campaigns/leads/send_queue/send_log from the previous version) are
    added here. Runs on every boot; zero manual SQL needed. Never touches
    the old Google-OAuth tables (users, gmail_accounts) or any existing data.
    """
    want = {
        "campaigns": ["followups_enabled INTEGER DEFAULT 1",
                      "followup_count INTEGER DEFAULT 5",
                      "followup_delay_hours INTEGER DEFAULT 40",
                      "autopilot INTEGER DEFAULT 0",
                      "followup_mode TEXT DEFAULT 'fixed'",
                      "max_touches INTEGER DEFAULT 7",
                      "pipeline_enabled INTEGER DEFAULT 0",
                      "pipeline_target_leads INTEGER DEFAULT 50",
                      "daily_discovery_target INTEGER DEFAULT 100",
                      "pipeline_stage TEXT DEFAULT ''",
                      "pipeline_cursor TEXT DEFAULT ''",
                      "dm_enabled INTEGER DEFAULT 1",
                      "dm_max_contacts INTEGER DEFAULT 3"],
        "send_queue": ["step INTEGER DEFAULT 0",
                       "contact_id INTEGER"],
        "send_log": ["step INTEGER DEFAULT 0",
                     "contact_id INTEGER"],
        "leads": ["replied INTEGER DEFAULT 0",
                  "personalized_line TEXT",
                  "handled INTEGER DEFAULT 0",
                  "ai_status TEXT DEFAULT ''",
                  "ai_note TEXT DEFAULT ''",
                  "unsubscribed INTEGER DEFAULT 0",
                  "fit TEXT DEFAULT ''",
                  "dm_status TEXT DEFAULT ''"],
    }
    for table, cols in want.items():
        for ddl in cols:
            _add_column(table, ddl)
    _ensure_notifications_table()
    _ensure_ai_content_table()
    _ensure_user_settings_table()
    _ensure_password_resets_table()
    _ensure_contacts_table()
    _ensure_app_users_email_unique()


def _ensure_contacts_table():
    """Idempotent CREATE TABLE for decision-maker contacts (covers
    databases that predate the table; create_all() covers fresh DBs)."""
    try:
        from sqlalchemy import text
        ddl = ("CREATE TABLE IF NOT EXISTS contacts ("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "lead_id INTEGER NOT NULL, name TEXT DEFAULT '', "
               "title TEXT DEFAULT '', email TEXT DEFAULT '', "
               "email_verdict TEXT DEFAULT '', "
               "email_verdict_detail TEXT DEFAULT '', "
               "source_url TEXT DEFAULT '', "
               "verified INTEGER NOT NULL DEFAULT 0, "
               "created_at FLOAT NOT NULL)")
        if IS_POSTGRES:
            ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                              "SERIAL PRIMARY KEY")
        with engine.begin() as con:
            con.execute(text(ddl))
    except Exception:
        pass


def _ensure_notifications_table():
    """Idempotent CREATE TABLE for databases that predate the notifications
    table (create_all() already covers fresh DBs; this covers the rest)."""
    try:
        from sqlalchemy import text
        ddl = ("CREATE TABLE IF NOT EXISTS notifications ("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "user_id INTEGER NOT NULL, lead_id INTEGER, campaign_id INTEGER, "
               "kind TEXT DEFAULT 'reply', title TEXT DEFAULT '', snippet TEXT DEFAULT '', "
               "created_at FLOAT NOT NULL, read_at FLOAT)")
        # Postgres uses SERIAL, not AUTOINCREMENT
        if IS_POSTGRES:
            ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                              "SERIAL PRIMARY KEY")
        with engine.begin() as con:
            con.execute(text(ddl))
    except Exception:
        pass


def _ensure_ai_content_table():
    """Idempotent CREATE TABLE for the autopilot cache (covers databases
    that predate the table; create_all() covers fresh DBs)."""
    try:
        from sqlalchemy import text
        ddl = ("CREATE TABLE IF NOT EXISTS ai_content ("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "lead_id INTEGER NOT NULL UNIQUE, subject TEXT DEFAULT '', "
               "body TEXT DEFAULT '', fu1_subj TEXT DEFAULT '', fu1_body TEXT DEFAULT '', "
               "fu2_subj TEXT DEFAULT '', fu2_body TEXT DEFAULT '', "
               "fu3_subj TEXT DEFAULT '', fu3_body TEXT DEFAULT '', "
               "findings_json TEXT DEFAULT '[]', "
               "tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, "
               "created_at FLOAT NOT NULL)")
        if IS_POSTGRES:
            ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                              "SERIAL PRIMARY KEY")
        with engine.begin() as con:
            con.execute(text(ddl))
    except Exception:
        pass


def _ensure_user_settings_table():
    """Idempotent CREATE TABLE for per-user compliance settings (covers
    databases that predate the table; create_all() covers fresh DBs)."""
    try:
        from sqlalchemy import text
        ddl = ("CREATE TABLE IF NOT EXISTS user_settings ("
               "user_id INTEGER PRIMARY KEY, "
               "company_name TEXT DEFAULT '', "
               "company_address TEXT DEFAULT '', "
               "updated_at FLOAT NOT NULL DEFAULT 0)")
        with engine.begin() as con:
            con.execute(text(ddl))
    except Exception:
        pass


def _ensure_password_resets_table():
    """Idempotent CREATE TABLE for password-reset tokens (covers databases
    that predate the table; create_all() covers fresh DBs)."""
    try:
        from sqlalchemy import text
        ddl = ("CREATE TABLE IF NOT EXISTS password_resets ("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "user_id INTEGER NOT NULL, email TEXT DEFAULT '', "
               "token_hash TEXT NOT NULL UNIQUE, "
               "created_at FLOAT NOT NULL, expires_at FLOAT NOT NULL, "
               "used_at FLOAT)")
        if IS_POSTGRES:
            ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                              "SERIAL PRIMARY KEY")
        with engine.begin() as con:
            con.execute(text(ddl))
    except Exception:
        pass


def _ensure_app_users_email_unique():
    """Add a UNIQUE constraint on app_users.email for databases whose table
    predates the constraint (fresh DBs already get it via create_all()).

    Never deletes data: while duplicate email rows exist (legacy), the
    constraint is skipped and app-level checks block new duplicates.
    Retried on every boot, so it lands automatically once dupes are gone.
    Returns True when the constraint was added by this call.
    """
    try:
        dupes = q("SELECT email FROM app_users GROUP BY email "
                  "HAVING COUNT(*) > 1 LIMIT 1")
        if dupes:
            return False
        if IS_POSTGRES:
            has = q("""SELECT 1 FROM information_schema.table_constraints tc
                       JOIN information_schema.key_column_usage kcu
                         ON tc.constraint_name = kcu.constraint_name
                        AND tc.table_schema = kcu.table_schema
                       WHERE tc.table_name = 'app_users'
                         AND tc.constraint_type = 'UNIQUE'
                         AND kcu.column_name = 'email' LIMIT 1""")
            if has:
                return False
            from sqlalchemy import text
            with engine.begin() as con:
                con.execute(text("ALTER TABLE app_users "
                                 "ADD CONSTRAINT app_users_email_unique UNIQUE (email)"))
            return True
        # SQLite: look for an existing unique index covering email.
        for ix in q("PRAGMA index_list(app_users)") or []:
            if ix.get("unique"):
                cols = q("PRAGMA index_info(%s)" % ix["name"])
                if cols and cols[0].get("name") == "email":
                    return False
        from sqlalchemy import text
        with engine.begin() as con:
            con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS "
                             "app_users_email_uidx ON app_users(email)"))
        return True
    except Exception:
        return False


def _add_column(table, ddl):
    name = ddl.split()[0]
    try:
        from sqlalchemy import text
        with engine.begin() as con:
            if IS_POSTGRES:
                con.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {ddl}"))
            else:
                existing = [r[1] for r in con.execute(
                    text(f"PRAGMA table_info({table})")).fetchall()]
                if name not in existing:
                    con.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    except Exception:
        pass


def _prep(sql):
    """Rewrite '?' placeholders as SQLAlchemy named binds (:p0, :p1, ...).
    text() only accepts named params in SQLAlchemy 2.x, on both SQLite and Postgres."""
    import re
    out, i = [], [0]

    def repl(_):
        name = f":p{i[0]}"
        i[0] += 1
        return name

    return re.sub(r"\?", repl, sql)


def _params(args):
    if isinstance(args, dict):
        return args
    return {f"p{i}": v for i, v in enumerate(args or ())}


def q(sql, args=(), one=False):
    from sqlalchemy import text
    with engine.connect() as con:
        rows = con.execute(text(_prep(sql)), _params(args))
        cols = rows.keys()
        out = [dict(zip(cols, r)) for r in rows.fetchall()]
    return (out[0] if out else None) if one else out


def w(sql, args=()):
    """Execute a write. Returns the inserted row id for INSERTs, else None."""
    from sqlalchemy import text
    sql_s = sql.strip()
    is_insert = sql_s[:6].upper() == "INSERT"
    stmt = _prep(sql)
    if IS_POSTGRES and is_insert and "returning" not in sql_s.lower():
        stmt += " RETURNING id"
    with engine.begin() as con:
        res = con.execute(text(stmt), _params(args))
        if is_insert:
            try:
                row = res.fetchone()  # Postgres RETURNING path
                if row:
                    return row[0]
            except Exception:
                pass
            try:
                return res.lastrowid  # SQLite path
            except Exception:
                return None
        return None


# ---------------- users (built-in auth) ----------------

def get_user(uid):
    return q("SELECT * FROM app_users WHERE id=?", (uid,), one=True)


def get_user_by_email(email):
    return q("SELECT * FROM app_users WHERE email=?", (email,), one=True)


def get_users_by_email(email):
    """All accounts sharing one email, oldest first (legacy duplicates)."""
    return q("SELECT * FROM app_users WHERE email=? ORDER BY id", (email,))


def create_user(email, name, password_hash):
    wid = w("INSERT INTO app_users (email, name, password_hash, created_at) VALUES (?,?,?,?)",
            (email, name or "", password_hash, time.time()))
    return q("SELECT * FROM app_users WHERE id=?", (wid,), one=True)


# ---------------- per-user compliance settings (email footer) ----------------

def get_user_settings(uid):
    """Return the user's compliance settings row, or None when unset."""
    return q("SELECT * FROM user_settings WHERE user_id=?", (uid,), one=True)


def save_user_settings(uid, company_name, company_address):
    """Upsert the user's company name + physical address for the footer."""
    existing = get_user_settings(uid)
    if existing:
        w("UPDATE user_settings SET company_name=?, company_address=?, updated_at=? WHERE user_id=?",
          (company_name or "", company_address or "", time.time(), uid))
    else:
        w("INSERT INTO user_settings (user_id, company_name, company_address, updated_at) VALUES (?,?,?,?)",
          (uid, company_name or "", company_address or "", time.time()))
    return get_user_settings(uid)
