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
import lead_quality as lq
import sender
import followups
import jobs
import pipeline

MAX_SENDS_PER_CAMPAIGN = 5
DRY_RUN_SENDS_PER_RUN = 25
MAX_RUN_SECONDS = 50  # stay well under serverless time limits


def enqueue_campaign(user_id, campaign_id):
    """Queue selected, VALIDATED leads for a campaign and flip it to sending.

    Only emails with a queueable verdict are queued: verdict must be
    "valid" or "risky" (see lead_quality: syntactically valid, non-disposable,
    domain has MX records, mailbox confirmed or catch-all). Leads with no
    email, or with an invalid/unknown/unvalidated verdict, are never queued.
    Unsubscribed, replied, and bounced leads are never queued (same as the
    reply/bounce guards in the pipeline and sequence_gate).

    When decision-maker enrichment found verified, mailable contacts for a
    lead, one queue row is inserted PER CONTACT; otherwise one row for the
    lead's general email (contact_id NULL)."""
    import lead_quality as lq
    import decision_makers as dmm
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (campaign_id,), one=True) or {}
    leads = db.q(
        "SELECT * FROM leads WHERE campaign_id=? AND selected=1 AND email<>'' "
        "AND email_verdict IN " + lq.queueable_verdict_sql() + " "
        "AND unsubscribed=0 AND replied=0 "
        "AND NOT EXISTS (SELECT 1 FROM send_log b "
        "WHERE b.campaign_id=? AND b.lead_id=leads.id "
        "AND b.status='bounced')",
        (campaign_id, campaign_id))
    now = time.time()
    n = 0
    dm_on = bool(int(camp.get("dm_enabled") or 0)) if camp.get("dm_enabled") is not None else True
    try:
        dm_max = max(1, min(4, int(camp.get("dm_max_contacts") or 3)))
    except (TypeError, ValueError):
        dm_max = 3
    for lead in leads:
        contacts = dmm.mailable_contacts(lead["id"], dm_max) if dm_on else []
        targets = [c["id"] for c in contacts] or [None]
        for contact_id in targets:
            exists = db.q("SELECT id FROM send_queue WHERE campaign_id=? AND lead_id=? "
                          "AND COALESCE(contact_id,0)=COALESCE(?,0)",
                          (campaign_id, lead["id"], contact_id or 0), one=True)
            if not exists:
                db.w("INSERT INTO send_queue (campaign_id, lead_id, contact_id, status, scheduled_at) VALUES (?,?,?, 'pending', ?)",
                     (campaign_id, lead["id"], contact_id, now))
                n += 1
    db.w("UPDATE campaigns SET status='sending', next_send_at=? WHERE id=?", (now, campaign_id))
    return n


def _schedule_until_reply(camp, lead, nxt, contact_id=None):
    """'Until they reply' mode: keep nudging every followup_delay_hours until
    max_touches total emails have gone out (steps 0..max_touches-1). Steps 1-3
    use the AI follow-ups; beyond that the 3rd follow-up cycles with a
    rotating prefix. Reply/bounce still stop everything via sequence_gate.
    Leads without AI content fall back to the fixed step list."""
    import ai_writer as _ai
    try:
        max_t = max(1, min(20, int(camp.get("max_touches") or 7)))
    except (TypeError, ValueError):
        max_t = 7
    if nxt >= max_t:
        return  # touches are steps 0..max_t-1; never exceed max_touches
    if not (camp.get("autopilot") and _ai.get_ai_content(lead["id"])):
        # No AI content for this lead: behave like fixed mode.
        count = followups.step_count(camp)
        if nxt > count:
            return
        fu = followups.get_followup(camp["id"], nxt)
        if not fu or not (fu["body_tpl"] or "").strip():
            return
    exists = db.q("SELECT id FROM send_queue WHERE campaign_id=? AND lead_id=? "
                  "AND COALESCE(contact_id,0)=COALESCE(?,0) AND step=?",
                  (camp["id"], lead["id"], contact_id or 0, nxt), one=True)
    if exists:
        return
    delay_h = followups.delay_hours(camp)
    db.w("INSERT INTO send_queue (campaign_id, lead_id, contact_id, status, scheduled_at, step)"
         " VALUES (?,?,?, 'pending', ?, ?)",
         (camp["id"], lead["id"], contact_id, time.time() + delay_h * 3600, nxt))


def _schedule_next_step(camp, lead, step, contact_id=None):
    """After step N sent, queue step N+1 for `followup_delay_hours` later.

    Only when follow-ups are enabled, the next step is within the campaign's
    step count, and a non-blank template exists for it. Never double-schedules.
    """
    if not camp.get("followups_enabled", 1):
        return
    nxt = (step or 0) + 1
    if (camp.get("followup_mode") or "fixed") == "until_reply":
        _schedule_until_reply(camp, lead, nxt, contact_id)
        return
    count = followups.step_count(camp)
    if nxt > count:
        return
    fu = followups.get_followup(camp["id"], nxt)
    if not fu or not (fu["body_tpl"] or "").strip():
        return  # chain ends at a blank step
    exists = db.q("SELECT id FROM send_queue WHERE campaign_id=? AND lead_id=? "
                  "AND COALESCE(contact_id,0)=COALESCE(?,0) AND step=?",
                  (camp["id"], lead["id"], contact_id or 0, nxt), one=True)
    if exists:
        return
    delay_h = followups.delay_hours(camp)
    db.w("INSERT INTO send_queue (campaign_id, lead_id, contact_id, status, scheduled_at, step)"
         " VALUES (?,?,?, 'pending', ?, ?)",
         (camp["id"], lead["id"], contact_id, time.time() + delay_h * 3600, nxt))


def _get_contact(item):
    """The verified decision-maker row for a queue item, or None when the
    item targets the lead's general email."""
    cid = (item or {}).get("contact_id")
    if not cid:
        return None
    return db.q("SELECT * FROM contacts WHERE id=?", (cid,), one=True)


def _process_campaign(camp, now_ts, send_fn=None):
    """Process due sends for one campaign. Returns list of outcome dicts.

    Per-domain spread: at most one email per recipient domain goes out per
    run, so a business's 3-4 decision makers are never blasted in a single
    tick; the rest wait for the next tick."""
    outcomes = []
    domains_sent = set()   # recipient domains already mailed this run
    deferred_ids = set()   # queue ids skipped this run (domain already sent)
    per_run = DRY_RUN_SENDS_PER_RUN if camp["dry_run"] else MAX_SENDS_PER_CAMPAIGN
    while len(outcomes) < per_run:
        if time.time() - now_ts > MAX_RUN_SECONDS:
            break
        now = time.time()
        not_in = (" AND id NOT IN (%s)" % ",".join(str(i) for i in sorted(deferred_ids))) if deferred_ids else ""
        item = db.q(
            "SELECT * FROM send_queue WHERE campaign_id=? AND status='pending' AND scheduled_at<=?" + not_in + " ORDER BY id LIMIT 1",
            (camp["id"], now), one=True)
        if not item:
            if deferred_ids:
                # Only domain-deferred items remain: they wait for the next
                # tick. Do not mark the campaign done/waiting.
                break
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
        contact = _get_contact(item)
        contact_id = (contact or {}).get("id")
        if contact and not contact.get("email"):
            # Contact lost its email (e.g. verdict turned invalid and the
            # address was cleared): never mail a guess, drop the row.
            db.w("UPDATE send_queue SET status='failed', last_error=? WHERE id=?",
                 ("Decision-maker contact has no public email; skipped.", item["id"]))
            continue
        recipient = ((contact or {}).get("email") or lead["email"] or "").strip()
        rdomain = recipient.partition("@")[2].lower()
        if contact_id and rdomain and rdomain in domains_sent:
            # Per-domain spread: this business already got one decision-maker
            # email this run; its other contacts wait for the next tick.
            # (General lead emails are not spread: gmail.com and friends
            # must not block each other.)
            deferred_ids.add(item["id"])
            continue
        if lead.get("unsubscribed"):
            # Belt-and-suspenders: a lead unsubscribed after being queued is
            # dropped here too, never mailed again.
            step = item["step"] or 0
            db.w("UPDATE send_queue SET status='failed', last_error=? WHERE id=?",
                 ("Lead unsubscribed; never mailed again.", item["id"]))
            outcomes.append({"campaign": camp["name"], "action": "sequence_stopped",
                             "to": lead["email"], "step": step,
                             "detail": "Lead unsubscribed; never mailed again."})
            continue
        if lead.get("replied"):
            # Belt-and-suspenders: a lead that replied after being queued is
            # dropped here too; the whole sequence stops, never mailed again.
            step = item["step"] or 0
            db.w("UPDATE send_queue SET status='failed', last_error=? WHERE id=?",
                 ("Lead replied; sequence stopped.", item["id"]))
            outcomes.append({"campaign": camp["name"], "action": "sequence_stopped",
                             "to": lead["email"], "step": step,
                             "detail": "Lead replied; sequence stopped."})
            continue
        if not lq.is_queueable_verdict(lead.get("email_verdict")):
            # Belt-and-suspenders: an unvalidated/invalid email must never
            # send, even if it reached the queue through an older path or a
            # verdict changed after queueing.
            step = item["step"] or 0
            db.w("UPDATE send_queue SET status='failed', last_error=? WHERE id=?",
                 ("Email not validated; skipped.", item["id"]))
            import crm as _crm
            _crm.log_event(lead["id"], "sequence_stopped",
                           f"Sequence stopped: email not validated ({lead.get('email_verdict') or 'unvalidated'}); skipped",
                           contact_id=contact_id, meta={"step": step})
            outcomes.append({"campaign": camp["name"], "action": "sequence_stopped",
                             "to": lead["email"], "step": step,
                             "detail": "Email not validated; skipped."})
            continue
        step = item["step"] or 0
        if not sender.in_window(camp, datetime.now()):
            nxt = sender.next_window_open(camp, datetime.now()).timestamp()
            db.w("UPDATE campaigns SET next_send_at=? WHERE id=?", (nxt, camp["id"]))
            outcomes.append({"campaign": camp["name"], "action": "waiting_for_window"})
            break
        if step > 0:
            allowed, reason = sender.sequence_gate(camp["user_id"], camp["id"], lead, step,
                                                   contact_id=contact_id)
            if not allowed:
                db.w("UPDATE send_queue SET status='failed', attempts=attempts+1, last_error=? WHERE id=?",
                     (reason[:300], item["id"]))
                import crm as _crm
                _crm.log_event(lead["id"], "sequence_stopped",
                               f"Sequence stopped at step {step}: {reason[:200]}",
                               contact_id=contact_id, meta={"step": step})
                outcomes.append({"campaign": camp["name"], "action": "sequence_stopped",
                                 "to": lead["email"], "step": step, "detail": reason})
                continue
        outcome = sender.send_one(camp["user_id"], camp, lead, send_fn=send_fn, step=step,
                                    contact=contact)
        outcome["step"] = step
        if outcome.get("ok"):
            db.w("UPDATE send_queue SET status='sent' WHERE id=?", (item["id"],))
            if contact_id and rdomain:
                domains_sent.add(rdomain)
            delay = outcome.get("delay_s", outcome.get("intended_delay_s", camp["delay_min"]))
            _schedule_next_step(camp, lead, step, contact_id)
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
    sequence promptly. Newly replied leads also get a notification row and
    an email to the user's login address. IMAP failures degrade to zero
    marked; never raises.
    """
    marked = 0
    try:
        accts = db.q("SELECT * FROM sender_accounts WHERE status='active'")
    except Exception:
        return 0
    for a in accts:
        try:
            import smtp_mail
            new_replies = sender.mark_replies(a["user_id"], smtp_mail.scan_replies(a))
            if new_replies:
                sender.notify_replies(a["user_id"], a, new_replies)
                marked += len(new_replies)
        except Exception:
            continue
    return marked


def process_all(send_fn=None):
    """One cron tick: reply scan + one job chunk + pipeline tick + all due sends.
    Returns a summary dict."""
    replies = _scan_replies_all()
    job_id = jobs.process_one_job_chunk()
    pipe = pipeline.tick_all()
    sends = process_sends(send_fn=send_fn)
    return {"job_chunk": job_id, "pipeline": pipe, "sends": sends,
            "replies_marked": replies,
            "processed_at": datetime.now().isoformat(timespec="seconds")}
