"""CRM event timeline: every touch on a lead is a stored event.

Instead of reconstructing history from counters, each action writes an
event row at the moment it happens: lead creation, enrichment, email
validation, queueing, every send (real or dry-run), follow-ups, replies,
bounces, unsubscribes, suppression and sequence stops. The lead page
renders these as a timeline; the campaign page shows a recent-activity
feed. Events are never fabricated after the fact.
"""
import json
import time
from datetime import datetime, timezone

import db

# The complete event vocabulary. Code paths must use exactly these types.
EVENT_TYPES = (
    "lead_created",
    "enriched",
    "decision_makers_found",
    "email_validated",
    "dry_run_queued",
    "email_sent",
    "followup_sent",
    "reply_detected",
    "bounced",
    "unsubscribed",
    "suppressed",
    "sequence_stopped",
)

# Display metadata for the timeline UI: (icon, short label).
EVENT_STYLE = {
    "lead_created": ("\U0001f195", "Lead created"),
    "enriched": ("\U0001f50d", "Enriched"),
    "decision_makers_found": ("\U0001f464", "Decision makers"),
    "email_validated": ("\u2705", "Email validated"),
    "dry_run_queued": ("\U0001f4cb", "Queued (dry-run)"),
    "email_sent": ("\U0001f4e4", "Email sent"),
    "followup_sent": ("\U0001f4e8", "Follow-up sent"),
    "reply_detected": ("\U0001f4ac", "Reply detected"),
    "bounced": ("\u26a0\ufe0f", "Bounced"),
    "unsubscribed": ("\U0001f6ab", "Unsubscribed"),
    "suppressed": ("\U0001f5d1\ufe0f", "Suppressed"),
    "sequence_stopped": ("\u23f9\ufe0f", "Sequence stopped"),
}


def log_event(lead_id, event_type, detail="", contact_id=None, meta=None):
    """Write one timeline event. Never raises: CRM logging must not break
    the action it records."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type!r}")
    try:
        db.w(
            "INSERT INTO lead_events (lead_id, contact_id, event_type, detail, meta, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (lead_id, contact_id, event_type, detail or "",
             json.dumps(meta or {}), time.time()))
    except Exception:
        pass


def fmt_time(ts):
    """Human timestamp for the timeline (server time, UTC on Vercel)."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%b %d, %H:%M UTC")
    except Exception:
        return ""


def lead_timeline(lead_id):
    """All events for one lead, newest first, with display metadata."""
    rows = db.q(
        "SELECT * FROM lead_events WHERE lead_id=? ORDER BY created_at DESC, id DESC",
        (lead_id,))
    out = []
    for r in rows:
        icon, label = EVENT_STYLE.get(r.get("event_type") or "", ("\u2022", "Event"))
        r = dict(r)
        r["icon"] = icon
        r["label"] = label
        r["ts"] = fmt_time(r.get("created_at"))
        try:
            r["meta_d"] = json.loads(r.get("meta") or "{}")
        except Exception:
            r["meta_d"] = {}
        out.append(r)
    return out


def campaign_activity(campaign_id, limit=15):
    """Latest events across a campaign's leads, newest first (one query).

    LEFT JOIN so suppression events stay visible after the lead row itself
    was deleted; the human-readable detail text still names the business."""
    rows = db.q(
        """SELECT e.*, l.campaign_id AS lead_campaign, l.business_name AS business_name
           FROM lead_events e LEFT JOIN leads l ON l.id = e.lead_id
           WHERE l.campaign_id=? OR (l.id IS NULL AND e.meta LIKE ?)
           ORDER BY e.created_at DESC, e.id DESC LIMIT ?""",
        (campaign_id, '%"campaign_id": ' + str(int(campaign_id)) + '%',
         int(limit or 15)))
    out = []
    for r in rows:
        icon, label = EVENT_STYLE.get(r.get("event_type") or "", ("\u2022", "Event"))
        r = dict(r)
        r["icon"] = icon
        r["label"] = label
        r["ts"] = fmt_time(r.get("created_at"))
        out.append(r)
    return out
