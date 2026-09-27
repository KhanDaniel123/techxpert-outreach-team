"""Sending engine: spintax, variables, account rotation, caps, warmup,
randomized delays, sending window, dry-run, send log, bounce auto-pause.

Sending goes through Gmail SMTP (smtp.gmail.com:587) using each sender
account's stored App Password - no Google OAuth involved.

Serverless note: there is no background thread here. queue_worker.process_sends()
is invoked by POST /api/process-queue (cron). Sending windows are evaluated in
server local time (UTC on Vercel); set windows accordingly (see README).
"""
import random
import re
import time
from datetime import datetime, timedelta

import db
import followups

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
        "personalized_line": lead.get("personalized_line") or "",
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
        db.w("UPDATE sender_accounts SET sent_today=0, sent_date=? WHERE id=?",
             (_today_str(), account["id"]))
        account = db.q("SELECT * FROM sender_accounts WHERE id=?", (account["id"],), one=True)
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
    accts = db.q("SELECT * FROM sender_accounts WHERE user_id=? AND status='active' ORDER BY id",
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
                subject, status, error="", dry_run=False, step=0):
    lid = db.w(
        """INSERT INTO send_log (user_id, campaign_id, account_id, lead_id, step,
           recipient, subject_rendered, sent_at, status, error, dry_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, campaign_id, account_id, lead_id, step, recipient, subject,
         time.time(), status, error, 1 if dry_run else 0))
    if not dry_run and status == "sent" and account_id:
        db.w("UPDATE sender_accounts SET sent_today = sent_today + 1 WHERE id=?", (account_id,))
    return lid


def _templates_for(campaign, step, lead=None):
    """Return (subject_tpl, body_tpl) for a queue step.
    Step 0 = the campaign's main template; steps 1..10 = follow-up rows.

    On autopilot campaigns, a lead with cached AI content gets the AI email
    (step 0) and AI follow-ups (steps 1-3). Steps beyond 3 cycle the 3rd
    follow-up with a rotating "circling back" prefix ({business_name} in the
    prefix is rendered by the caller's render_template). Leads without AI
    content fall back to the normal templates."""
    if campaign.get("autopilot") and lead:
        import ai_writer as _ai
        ai = _ai.get_ai_content(lead["id"])
        if ai and (ai.get("subject") or ai.get("body")):
            if not step:
                return ai["subject"] or "", ai["body"] or ""
            if 1 <= step <= 3:
                subj = ai.get(f"fu{step}_subj") or ""
                body = ai.get(f"fu{step}_body") or ""
                if subj or body:
                    return subj, body
            else:
                prefixes = _ai.FOLLOWUP_CYCLE_PREFIXES
                prefix = prefixes[(step - 4) % len(prefixes)]
                subj = ai.get("fu3_subj") or ""
                body = prefix + (ai.get("fu3_body") or "")
                if subj or body:
                    return subj, body
    if step and step > 0:
        fu = db.q("SELECT * FROM followups WHERE campaign_id=? AND step=?",
                  (campaign["id"], step), one=True)
        if not fu or not (fu["body_tpl"] or "").strip():
            return None, None
        return fu["subject_tpl"] or "", fu["body_tpl"]
    return campaign["subject_tpl"], campaign["body_tpl"]


def sequence_gate(user_id, campaign_id, lead, step):
    """Decide whether follow-up `step` (>0) may be sent to this lead.

    Rules: follow-ups are only enabled when the campaign says so, and a
    follow-up only sends if the previous step sent successfully AND no reply
    was detected AND the address did not bounce.
    Returns (allowed: bool, reason: str).
    """
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campaign_id,), one=True)
    if camp and not followups.is_enabled(camp):
        return False, "Follow-ups are disabled for this campaign."
    if lead.get("replied"):
        return False, "Lead replied; sequence stopped."
    if lead.get("unsubscribed"):
        return False, "Lead unsubscribed; sequence stopped."
    bounced = db.q(
        "SELECT id FROM send_log WHERE campaign_id=? AND lead_id=? AND status='bounced' LIMIT 1",
        (campaign_id, lead["id"]), one=True)
    if bounced:
        return False, "Address bounced; sequence stopped."
    prev = db.q(
        "SELECT id FROM send_log WHERE campaign_id=? AND lead_id=? AND step=? AND status IN ('sent','dry-run') LIMIT 1",
        (campaign_id, lead["id"], step - 1), one=True)
    if not prev:
        return False, f"Previous step ({step - 1}) did not send successfully; sequence stopped."
    return True, ""


def mark_replies(user_id, replies):
    """Mark leads as replied for any matching address.

    `replies`: list of dicts {address, snippet, date} from
    smtp_mail.scan_replies (plain address strings also accepted).
    Returns a list of dicts for NEWLY-marked leads only (a lead already
    marked replied is never returned twice):
    {lead_id, business_name, email, campaign_id, campaign_name,
     snippet, reply_date}.
    """
    newly = []
    seen = set()
    for r in replies or []:
        if isinstance(r, dict):
            addr = (r.get("address") or "").lower()
            snippet = r.get("snippet") or ""
            rdate = r.get("date")
        else:
            addr = (r or "").lower()
            snippet, rdate = "", None
        if "@" not in addr or addr in seen:
            continue
        seen.add(addr)
        rows = db.q("SELECT id, business_name, email, campaign_id FROM leads "
                    "WHERE user_id=? AND lower(email)=? AND replied=0",
                    (user_id, addr))
        for row in rows:
            db.w("UPDATE leads SET replied=1 WHERE id=?", (row["id"],))
            camp = None
            if row["campaign_id"]:
                camp = db.q("SELECT id, name FROM campaigns WHERE id=?",
                            (row["campaign_id"],), one=True)
            newly.append({
                "lead_id": row["id"],
                "business_name": row["business_name"] or "",
                "email": row["email"] or "",
                "campaign_id": row["campaign_id"],
                "campaign_name": (camp["name"] if camp else "") or "",
                "snippet": snippet,
                "reply_date": rdate,
            })
    return newly


def notify_replies(user_id, account, new_replies):
    """For each newly-marked replied lead: store one notification row
    (deduped per lead) and email the user at their login address from their
    sender account.

    Never raises. If there is no sender account to send from (or no login
    email on file), the notification is still stored and the email is
    skipped gracefully. Returns the number of notification rows created.
    """
    import config
    created = 0
    user = db.get_user(user_id)
    user_email = ((user or {}).get("email") or "").strip()
    app_url = (config.APP_URL or "").rstrip("/")
    for nr in new_replies or []:
        exists = db.q("SELECT id FROM notifications WHERE user_id=? AND lead_id=? "
                      "AND kind='reply' LIMIT 1",
                      (user_id, nr["lead_id"]), one=True)
        if not exists:
            title = f"Reply from {nr['business_name'] or nr['email']}"
            db.w("""INSERT INTO notifications
                    (user_id, lead_id, campaign_id, kind, title, snippet, created_at, read_at)
                    VALUES (?,?,?,?,?,?,?,NULL)""",
                 (user_id, nr["lead_id"], nr["campaign_id"], "reply",
                  title, nr["snippet"] or "", time.time()))
            created += 1
        if account and user_email:
            try:
                import smtp_mail
                who = nr["business_name"] or nr["email"]
                camp = nr["campaign_name"] or "your campaign"
                subject = f"Reply from {who} ({camp})"
                link = (f"{app_url}/campaign/{nr['campaign_id']}"
                        if nr.get("campaign_id") else f"{app_url}/dashboard")
                body = (
                    f"{who} ({nr['email']}) replied to your email"
                    + (f" in campaign \"{nr['campaign_name']}\"" if nr["campaign_name"] else "")
                    + ".\n\n--- reply snippet ---\n"
                    + ((nr["snippet"] or "(no text captured)")[:1500])
                    + f"\n\nMove it forward yourself: {link}\n"
                )
                smtp_mail.send_message(account, user_email, subject, body)
            except Exception:
                pass
    return created


def send_one(user_id, campaign, lead, send_fn=None, rng=None, step=0):
    """Send (or dry-run) a single message. send_fn(account, to, subject, body)
    defaults to the real Gmail SMTP path. `step` selects the template:
    0 = main template, 1..10 = follow-up step. Returns dict with outcome details."""
    rng = rng or random
    account = pick_account(user_id, campaign)
    if not account:
        return {"ok": False, "reason": "no_eligible_account",
                "detail": "No active sender account under its daily cap inside the sending window."}

    subject_tpl, body_tpl = _templates_for(campaign, step, lead)
    if body_tpl is None:
        return {"ok": False, "reason": "no_followup_template",
                "detail": f"Follow-up step {step} has no template; sequence ends here."}

    tpl_ctx = dict(lead)
    subject = render_template(subject_tpl, tpl_ctx, rng)
    body = render_template(body_tpl, tpl_ctx, rng)
    recipient = (lead["email"] or "").strip()
    if not recipient:
        return {"ok": False, "reason": "no_email", "detail": "Lead has no email address."}
    if lead.get("unsubscribed"):
        return {"ok": False, "reason": "unsubscribed",
                "detail": "Lead unsubscribed; never mailed again."}

    # Compliance footer on every outgoing campaign email and follow-up:
    # company name + physical address + one-click unsubscribe link.
    import unsubscribe as _unsub
    footer = _unsub.build_footer(user_id, lead["id"])
    body = body + footer
    list_unsub_url = _unsub.unsubscribe_url(lead["id"], user_id)

    intended_delay = rng.randint(campaign["delay_min"], campaign["delay_max"])

    if campaign["dry_run"]:
        record_send(user_id, campaign["id"], account["id"], lead["id"],
                    recipient, subject, status="dry-run", dry_run=True, step=step)
        return {"ok": True, "dry_run": True, "account": account["email"],
                "to": recipient, "subject": subject, "step": step,
                "intended_delay_s": intended_delay,
                "note": "Dry-run: nothing was sent. Delay simulated, not slept."}

    try:
        if send_fn is None:
            import smtp_mail
            smtp_mail.send_message(account, recipient, subject, body,
                                   list_unsub_url=list_unsub_url)
        else:
            send_fn(account, recipient, subject, body)
        record_send(user_id, campaign["id"], account["id"], lead["id"],
                    recipient, subject, status="sent", step=step)
        return {"ok": True, "account": account["email"], "to": recipient,
                "subject": subject, "step": step, "delay_s": intended_delay}
    except Exception as e:
        err = str(e)[:300]
        record_send(user_id, campaign["id"], account["id"], lead["id"],
                    recipient, subject, status="failed", error=err, step=step)
        return {"ok": False, "reason": "send_failed", "detail": err}


def check_account_bounces(user_id, account_id, bounce_addrs=None):
    """Scan recent bounces via IMAP; mark log rows; auto-pause account if
    bounce rate spikes.

    bounce_addrs: override list (used by tests). Otherwise uses the IMAP scan.
    Returns dict with bounced count, rate, paused flag.
    """
    if bounce_addrs is None:
        import smtp_mail
        account = db.q("SELECT * FROM sender_accounts WHERE id=?", (account_id,), one=True)
        bounce_addrs = smtp_mail.scan_bounces(account) if account else []
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
        db.w("UPDATE sender_accounts SET status='paused' WHERE id=?", (account_id,))
        paused = True
    return {"bounced_marked": marked, "bounce_rate": round(rate, 3),
            "looked_at": len(recent), "paused": paused}
