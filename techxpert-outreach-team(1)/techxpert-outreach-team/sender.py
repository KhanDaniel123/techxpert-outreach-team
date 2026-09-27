"""Sending engine: spintax, variables, account rotation, caps, warmup,
randomized delays, sending window, dry-run, send log, bounce auto-pause.

Serverless note: there is no background thread here. queue_worker.process_sends()
is invoked by POST /api/process-queue (cron). Sending windows are evaluated in
server local time (UTC on Vercel); set windows accordingly (see README).
"""
import random
import re
import time
from datetime import datetime, timedelta

import db

SPINTAX_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")
VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

WARMUP_DAYS = 21
WARMUP_START_CAP = 5
BOUNCE_WINDOW = 50          # look at last N sends per account
BOUNCE_PAUSE_RATE = 0.10    # pause account if bounce rate exceeds this


def resolve_spintax(text, rng=None):
    """{opt one|opt two} -> randomly one option. Per-message randomness."""
    rng = rng or random
    def pick(m):
        return rng.choice(m.group(1).split("|"))
    prev = None
    out = text or ""
    while prev != out:  # handle nested
        prev = out
        out = SPINTAX_RE.sub(pick, out)
    return out


def render_template(tpl, lead, rng=None):
    """Resolve spintax first, then {variables} from the lead row."""
    text = resolve_spintax(tpl, rng)
    ctx = {
        "business_name": lead["business_name"] or "",
        "address": lead["address"] or "",
        "phone": lead["phone"] or "",
        "website": lead["website"] or "",
        "email": lead["email"] or "",
        "category": lead["category"] or "",
        "niche": "",
        "location": "",
    }
    def sub(m):
        return str(ctx.get(m.group(1), m.group(0)))
    return VAR_RE.sub(sub, text)


def account_age_days(account):
    return max(0, (time.time() - (account["warmup_start"] or account["created_at"])) / 86400.0)


def effective_cap(account):
    """Warmup: new accounts start at 5/day, ramp linearly to daily_cap over ~21 days."""
    cap = account["daily_cap"] or 30
    if not account["warmup_enabled"]:
        return cap
    age = account_age_days(account)
    if age >= WARMUP_DAYS:
        return cap
    ramped = WARMUP_START_CAP + (cap - WARMUP_START_CAP) * (age / WARMUP_DAYS)
    return max(WARMUP_START_CAP, min(cap, int(ramped)))


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def reset_daily_counter_if_needed(account):
    if account["sent_date"] != _today_str():
        db.w("UPDATE gmail_accounts SET sent_today=0, sent_date=? WHERE id=?",
             (_today_str(), account["id"]))
        account = db.q("SELECT * FROM gmail_accounts WHERE id=?", (account["id"],), one=True)
    return account


def in_window(campaign, now=None):
    now = now or datetime.now()
    try:
        s = datetime.strptime(campaign["window_start"], "%H:%M").time()
        e = datetime.strptime(campaign["window_end"], "%H:%M").time()
    except Exception:
        return True
    t = now.time()
    if s <= e:
        return s <= t <= e
    return t >= s or t <= e  # overnight window


def next_window_open(campaign, now=None):
    """Next datetime inside the sending window (for scheduling)."""
    now = now or datetime.now()
    try:
        s = datetime.strptime(campaign["window_start"], "%H:%M").time()
    except Exception:
        return now
    if in_window(campaign, now):
        return now
    cand = now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand


def eligible_accounts(user_id, campaign):
    accts = db.q("SELECT * FROM gmail_accounts WHERE user_id=? AND status='active' ORDER BY id",
                 (user_id,))
    out = []
    for a in accts:
        a = reset_daily_counter_if_needed(a)
        if a["sent_today"] < effective_cap(a) and in_window(campaign):
            out.append(a)
    return out


def pick_account(user_id, campaign):
    """Round-robin across eligible accounts."""
    eligible = eligible_accounts(user_id, campaign)
    if not eligible:
        return None
    last_id = campaign["last_account_id"] or 0
    ids = [a["id"] for a in eligible]
    if last_id in ids:
        nxt = ids[(ids.index(last_id) + 1) % len(ids)]
    else:
        nxt = ids[0]
    chosen = [a for a in eligible if a["id"] == nxt][0]
    db.w("UPDATE campaigns SET last_account_id=? WHERE id=?", (nxt, campaign["id"]))
    # keep the caller's in-memory campaign dict in sync so back-to-back calls rotate
    try:
        campaign["last_account_id"] = nxt
    except Exception:
        pass
    return chosen


def record_send(user_id, campaign_id, account_id, lead_id, recipient,
                subject, status, error="", dry_run=False):
    lid = db.w(
        """INSERT INTO send_log (user_id, campaign_id, account_id, lead_id,
           recipient, subject_rendered, sent_at, status, error, dry_run)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (user_id, campaign_id, account_id, lead_id, recipient, subject,
         time.time(), status, error, 1 if dry_run else 0))
    if not dry_run and status == "sent" and account_id:
        db.w("UPDATE gmail_accounts SET sent_today = sent_today + 1 WHERE id=?", (account_id,))
    return lid


def send_one(user_id, campaign, lead, send_fn=None, rng=None):
    """Send (or dry-run) a single message. send_fn(account, to, subject, body)
    defaults to the real Gmail API path. Returns dict with outcome details."""
    rng = rng or random
    account = pick_account(user_id, campaign)
    if not account:
        return {"ok": False, "reason": "no_eligible_account",
                "detail": "No active Gmail account under its daily cap inside the sending window."}

    tpl_ctx = dict(lead)
    subject = render_template(campaign["subject_tpl"], tpl_ctx, rng)
    body = render_template(campaign["body_tpl"], tpl_ctx, rng)
    recipient = (lead["email"] or "").strip()
    if not recipient:
        return {"ok": False, "reason": "no_email", "detail": "Lead has no email address."}

    intended_delay = rng.randint(campaign["delay_min"], campaign["delay_max"])

    if campaign["dry_run"]:
        record_send(user_id, campaign["id"], account["id"], lead["id"],
                    recipient, subject, status="dry-run", dry_run=True)
        return {"ok": True, "dry_run": True, "account": account["email"],
                "to": recipient, "subject": subject,
                "intended_delay_s": intended_delay,
                "note": "Dry-run: nothing was sent. Delay simulated, not slept."}

    try:
        if send_fn is None:
            import gmail_oauth
            service = gmail_oauth.get_service(account)
            gmail_oauth.send_message(service, recipient, subject, body)
        else:
            send_fn(account, recipient, subject, body)
        record_send(user_id, campaign["id"], account["id"], lead["id"],
                    recipient, subject, status="sent")
        return {"ok": True, "account": account["email"], "to": recipient,
                "subject": subject, "delay_s": intended_delay}
    except Exception as e:
        err = str(e)[:300]
        record_send(user_id, campaign["id"], account["id"], lead["id"],
                    recipient, subject, status="failed", error=err)
        return {"ok": False, "reason": "send_failed", "detail": err}


def check_account_bounces(user_id, account_id, service=None, bounce_addrs=None):
    """Scan recent bounces; mark log rows; auto-pause account if bounce rate spikes.

    bounce_addrs: override list (used by tests). Otherwise uses Gmail API scan.
    Returns dict with bounced count, rate, paused flag.
    """
    if bounce_addrs is None:
        import gmail_oauth
        account = db.q("SELECT * FROM gmail_accounts WHERE id=?", (account_id,), one=True)
        service = gmail_oauth.get_service(account) if service is None else service
        bounce_addrs = gmail_oauth.scan_bounces(service)
    bounce_set = set(a.lower() for a in bounce_addrs)

    marked = 0
    for row in db.q(
            "SELECT id, recipient FROM send_log WHERE account_id=? AND status='sent' ORDER BY id DESC LIMIT ?",
            (account_id, BOUNCE_WINDOW * 2)):
        if row["recipient"].lower() in bounce_set:
            db.w("UPDATE send_log SET status='bounced' WHERE id=?", (row["id"],))
            marked += 1

    recent = db.q(
        "SELECT status FROM send_log WHERE account_id=? AND dry_run=0 ORDER BY id DESC LIMIT ?",
        (account_id, BOUNCE_WINDOW))
    if recent:
        bounced = sum(1 for r in recent if r["status"] == "bounced")
        rate = bounced / len(recent)
    else:
        rate = 0.0

    paused = False
    if rate > BOUNCE_PAUSE_RATE and len(recent) >= 10:
        db.w("UPDATE gmail_accounts SET status='paused' WHERE id=?", (account_id,))
        paused = True
    return {"bounced_marked": marked, "bounce_rate": round(rate, 3),
            "looked_at": len(recent), "paused": paused}
