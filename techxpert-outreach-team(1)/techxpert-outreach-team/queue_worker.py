"""Serverless queue processing.

The local app ran a background thread polling every 15s. On Vercel there are
no persistent threads, so sending is driven by POST /api/process-queue,
invoked on a schedule (cron-job.org every 10 minutes recommended; Vercel's
built-in cron is only a backup).

Each invocation:
  1. runs ONE chunk of the oldest running background job (websearch/enrich/validate)
  2. processes due sends: up to MAX_SENDS_PER_CAMPAIGN live sends per campaign
     (dry-run campaigns: up to DRY_RUN_SENDS_PER_RUN, they're instant)

Honest cadence math: with the endpoint hit every 10 minutes, a campaign sends
at most 5 live messages per tick per campaign (~30/hour/campaign), spaced by
the campaign's configured random delay between ticks. This is the price of
serverless; a VPS with a real worker would be smoother. See README.
"""
import random
import time
from datetime import datetime

import db
import sender
import followups
import jobs

MAX_SENDS_PER_CAMPAIGN = 5
DRY_RUN_SENDS_PER_RUN = 25
MAX_RUN_SECONDS = 50  # stay well under serverless time limits


def enqueue_campaign(user_id, campaign_id):
    """Queue every selected lead with an email for a campaign."""
    leads = db.q(
        "SELECT * FROM leads WHERE campaign_id=? AND selected=1 AND email<>''",
        (campaign_id,))
    now = time.time()
    n = 0
    for lead in leads:
        exists = db.q("SELECT id FROM send_queue WHERE campaign_id=? AND lead_id=?",
                      (campaign_id, lead["id"]), one=True)
        if not exists:
            db.w("INSERT INTO send_queue (campaign_id, lead_id, status, scheduled_at) VALUES (?,?, 'pending', ?)",
                 (campaign_id, lead["id"], now))
            n += 1
    db.w("UPDATE campaigns SET status='sending', next_send_at=? WHERE id=?", (now, campaign_id))
    return n


def _schedule_next_step(camp, lead, step):
    """After step N sent, queue step N+1 for `followup_delay_hours` later.

    Only when follow-ups are enabled, the next step is within the campaign's
    step count, and a non-blank template exists for it. Never double-schedules.
    """
    if not camp.get("followups_enabled", 1):
        return
    nxt = (step or 0) + 1
    count = followups.step_count(camp)
    if nxt > count:
        return
    fu = followups.get_followup(camp["id"], nxt)
    if not fu or not (fu["body_tpl"] or "").strip():
        return  # chain ends at a blank step
    exists = db.q("SELECT id FROM send_queue WHERE campaign_id=? AND lead_id=? AND step=?",
                  (camp["id"], lead["id"], nxt), one=True)
    if exists:
        return
    delay_h = followups.delay_hours(camp)
    db.w("INSERT INTO send_queue (campaign_id, lead_id, status, scheduled_at, step)"
         " VALUES (?,?, 'pending', ?, ?)",
         (camp["id"], lead["id"], time.time() + delay_h * 3600, nxt))


def _process_campaign(camp, now_ts, send_fn=None):
    """Process due sends for one campaign. Returns list of outcome dicts."""
    outcomes = []
    per_run = DRY_RUN_SENDS_PER_RUN if camp["dry_run"] else MAX_SENDS_PER_CAMPAIGN
    while len(outcomes) < per_run:
        if time.time() - now_ts > MAX_RUN_SECONDS:
            break
        now = time.time()
        item = db.q(
            "SELECT * FROM send_queue WHERE campaign_id=? AND status='pending' AND scheduled_at<=? ORDER BY id LIMIT 1",
            (camp["id"], now), one=True)
        if not item:
            # Future follow-ups may still be pending: keep the campaign alive
            # and wake it when the next one is due; only mark done when the
            # queue is truly empty.
            nxt_pending = db.q(
                "SELECT MIN(scheduled_at) m FROM send_queue WHERE campaign_id=? AND status='pending'",
                (camp["id"],), one=True)
            if nxt_pending and nxt_pending["m"]:
                db.w("UPDATE campaigns SET next_send_at=? WHERE id=?",
                     (nxt_pending["m"], camp["id"]))
                outcomes.append({"campaign": camp["name"], "action": "waiting_for_followups"})
            else:
                db.w("UPDATE campaigns SET status='done' WHERE id=?", (camp["id"],))
            break
        lead = db.q("SELECT * FROM leads WHERE id=?", (item["lead_id"],), one=True)
        if not lead:
            db.w("UPDATE send_queue SET status='failed', last_error=? WHERE id=?",
                 ("lead missing", item["id"]))
            continue
        step = item["step"] or 0
        if not sender.in_window(camp, datetime.now()):
            nxt = sender.next_window_open(camp, datetime.now()).timestamp()
            db.w("UPDATE campaigns SET next_send_at=? WHERE id=?", (nxt, camp["id"]))
            outcomes.append({"campaign": camp["name"], "action": "waiting_for_window"})
            break
        if step > 0:
            allowed, reason = sender.sequence_gate(camp["user_id"], camp["id"], lead, step)
            if not allowed:
                db.w("UPDATE send_queue SET status='failed', attempts=attempts+1, last_error=? WHERE id=?",
                     (reason[:300], item["id"]))
                outcomes.append({"campaign": camp["name"], "action": "sequence_stopped",
                                 "to": lead["email"], "step": step, "detail": reason})
                continue
        outcome = sender.send_one(camp["user_id"], camp, lead, send_fn=send_fn, step=step)
        outcome["step"] = step
        if outcome.get("ok"):
            db.w("UPDATE send_queue SET status='sent' WHERE id=?", (item["id"],))
            delay = outcome.get("delay_s", outcome.get("intended_delay_s", camp["delay_min"]))
            _schedule_next_step(camp, lead, step)
        else:
            reason = outcome.get("reason", "")
            if reason == "no_eligible_account":
                # caps exhausted: retry on the next cron tick, don't burn the lead
                db.w("UPDATE campaigns SET next_send_at=? WHERE id=?",
                     (time.time() + 600, camp["id"]))
                outcomes.append({"campaign": camp["name"], "action": "paused_waiting",
                                 "detail": outcome.get("detail")})
                break
            db.w("UPDATE send_queue SET status='failed', attempts=attempts+1, last_error=? WHERE id=?",
                 (outcome.get("detail", "")[:300], item["id"]))
            delay = camp["delay_min"]
        nxt = time.time() + (delay if not camp["dry_run"] else 2)
        if not sender.in_window(camp, datetime.fromtimestamp(nxt)):
            nxt = sender.next_window_open(camp, datetime.fromtimestamp(nxt)).timestamp()
        db.w("UPDATE campaigns SET next_send_at=? WHERE id=?", (nxt, camp["id"]))
        outcome["campaign"] = camp["name"]
        outcomes.append({k: outcome[k] for k in ("campaign", "ok", "account", "to", "subject",
                                                "reason", "detail", "dry_run", "action", "step")
                         if k in outcome})
    return outcomes


def process_sends(now_ts=None, send_fn=None):
    """Process all due sends across sending campaigns. Returns outcome list."""
    now_ts = now_ts or time.time()
    start = now_ts
    outcomes = []
    campaigns = db.q("SELECT * FROM campaigns WHERE status='sending'")
    for camp in campaigns:
        if time.time() - start > MAX_RUN_SECONDS:
            outcomes.append({"action": "time_budget_reached"})
            break
        if now_ts < (camp["next_send_at"] or 0):
            continue
        try:
            outcomes.extend(_process_campaign(camp, start, send_fn=send_fn))
        except Exception as e:
            outcomes.append({"campaign": camp["name"], "action": "error",
                             "detail": str(e)[:200]})
    return outcomes


def _scan_replies_all():
    """Best-effort reply detection across all active sender accounts.

    Runs on every scheduler tick so a lead's reply stops their follow-up
    sequence promptly. IMAP failures degrade to zero marked; never raises.
    """
    marked = 0
    try:
        accts = db.q("SELECT * FROM sender_accounts WHERE status='active'")
    except Exception:
        return 0
    for a in accts:
        try:
            import smtp_mail
            addrs = smtp_mail.scan_replies(a)
            if addrs:
                marked += sender.mark_replies(a["user_id"], addrs)
        except Exception:
            continue
    return marked


def process_all(send_fn=None):
    """One cron tick: reply scan + one job chunk + all due sends.
    Returns a summary dict."""
    replies = _scan_replies_all()
    job_id = jobs.process_one_job_chunk()
    sends = process_sends(send_fn=send_fn)
    return {"job_chunk": job_id, "sends": sends, "replies_marked": replies,
            "processed_at": datetime.now().isoformat(timespec="seconds")}
