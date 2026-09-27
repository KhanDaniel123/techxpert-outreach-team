"""Database layer: Postgres (Neon) via SQLAlchemy when DATABASE_URL is set,
local SQLite fallback for development.

Keeps the same tiny interface as the original local app so the ported
modules (sender, leads, jobs, queue_worker) barely change:

    q(sql, args=(), one=False)  -> list of dicts (or single dict / None)
    w(sql, args=())             -> last inserted row id (for INSERTs)
    init_db()                   -> create all tables if missing

Write SQL with `?` placeholders; they are translated to `%s` on Postgres.
"""
import os
import time

from sqlalchemy import (create_engine, MetaData, Table, Column, Integer,
                        BigInteger, Text, Float, text as _sd)

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

users = Table("users", metadata,
              Column("id", Integer, primary_key=True, autoincrement=True),
              Column("google_sub", Text, unique=True, nullable=False),
              Column("email", Text, nullable=False),
              Column("name", Text, default=""),
              Column("created_at", Float, nullable=False))

gmail_accounts = Table("gmail_accounts", metadata,
                       Column("id", Integer, primary_key=True, autoincrement=True),
                       Column("user_id", Integer, nullable=False),
                       Column("email", Text, nullable=False),
                       Column("token_enc", Text, nullable=False),
                       Column("daily_cap", Integer, nullable=False, server_default="30"),
                       Column("warmup_enabled", Integer, nullable=False, server_default="1"),
                       Column("warmup_start", Float, nullable=False),
                       Column("status", Text, nullable=False, server_default=_sd("'active'")),
                       Column("sent_today", Integer, nullable=False, server_default="0"),
                       Column("sent_date", Text, nullable=False, server_default=_sd("''")),
                       Column("last_account_note", Text, default=""),
                       Column("created_at", Float, nullable=False))

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
                  Column("status", Text, nullable=False, server_default=_sd("'draft'")),
                  Column("next_send_at", Float),  # nullable; worker treats None as 0
                  Column("last_account_id", Integer, default=0),
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
              Column("created_at", Float, nullable=False))

send_queue = Table("send_queue", metadata,
                   Column("id", Integer, primary_key=True, autoincrement=True),
                   Column("campaign_id", Integer, nullable=False),
                   Column("lead_id", Integer, nullable=False),
                   Column("status", Text, nullable=False, server_default=_sd("'pending'")),
                   Column("scheduled_at", Float, nullable=False, server_default="0"),
                   Column("attempts", Integer, nullable=False, server_default="0"),
                   Column("last_error", Text, default=""))

send_log = Table("send_log", metadata,
                 Column("id", Integer, primary_key=True, autoincrement=True),
                 Column("user_id", Integer, nullable=False),
                 Column("campaign_id", Integer),
                 Column("account_id", Integer),
                 Column("lead_id", Integer),
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


def init_db():
    metadata.create_all(engine)


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


# ---------------- users ----------------

def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def get_or_create_user(google_sub, email, name):
    row = q("SELECT * FROM users WHERE google_sub=?", (google_sub,), one=True)
    if row:
        # keep name/email fresh
        w("UPDATE users SET email=?, name=? WHERE id=?", (email, name or "", row["id"]))
        return q("SELECT * FROM users WHERE id=?", (row["id"],), one=True)
    wid = w("INSERT INTO users (google_sub, email, name, created_at) VALUES (?,?,?,?)",
            (google_sub, email, name or "", time.time()))
    return q("SELECT * FROM users WHERE id=?", (wid,), one=True)
