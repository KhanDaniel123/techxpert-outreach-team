"""Follow-up sequences: up to 10 follow-up steps per campaign.

Each step has its own subject/body template (same {variables} and {spintax}
as the main template). Steps send one at a time, each `followup_delay_hours`
(per-campaign, default 40) after the previous step sent, and only if the lead
has not replied and the address did not bounce.

Follow-ups can be enabled/disabled per campaign and the step count set from
1 to 10 (default 5). Defaults are seeded at campaign creation so sequences
work out of the box. Blanking a step's body disables that step (and
everything after it).
"""
import db

MAX_STEPS = 10
DEFAULT_DELAY_HOURS = 40
DEFAULT_COUNT = 5

DEFAULT_FOLLOWUPS = [
    (1,
     "Bumping this up, {business_name}",
     "Hi {business_name} team,\n\n{Bumping this up in case it got buried|Circling back in case my note got lost in the shuffle}.\n\n{Still worth a 10-minute chat?|Open to a quick 10-minute chat this week?}\n\nBest"),
    (2,
     "Should I close the loop, {business_name}?",
     "Hi {business_name} team,\n\n{I'll keep this short|Quick one} - {should I close the loop on this?|want me to stop following up?}\n\n{No hard feelings either way|Totally fine if the timing is off}.\n\nBest"),
    (3,
     "{business_name}",
     "Hi {business_name} team,\n\n{Last note from me|Final nudge, I promise}. {If getting more {niche} clients without adding headcount is on your mind|If client flow is on your mind at all}, {we should talk|it is worth 10 minutes}.\n\n{If not, I'll stop here|Otherwise I'll close the loop}.\n\nBest"),
    (4,
     "One more for {business_name}",
     "Hi {business_name} team,\n\n{You're still on my follow-up list|One more from me}: {what's eating more of your week right now, finding clients or running the operation?|what's the bigger bottleneck right now, getting clients in or handling the work?}\n\n{Reply with one line and I'll tell you straight if we can help|One honest answer and I'll point you in the right direction}.\n\nBest"),
    (5,
     "Closing the loop",
     "Hi {business_name} team,\n\n{Closing the loop on my end|I'll stop reaching out after this}. {If timing changes, just reply and we'll talk|If things change, you know where to find me}.\n\n{Wishing you a strong quarter|Best of luck}.\n\nBest"),
    (6,
     "{business_name} - quick thought",
     "Hi {business_name} team,\n\n{Had a quick thought about {business_name}|Something came to mind about {business_name}} - {most {niche} teams I talk to leak clients between first contact and booking|most {niche} businesses I talk to lose clients in the follow-up gap}.\n\n{That's the gap we close|We fix exactly that}. {Worth 10 minutes?|Open to a quick look?}\n\nBest"),
    (7,
     "For {business_name}, whenever",
     "Hi {business_name} team,\n\n{No pitch this time|Keeping this one short} - {if getting clients ever feels like the bottleneck, just reply|if client acquisition ever becomes the bottleneck, reply and I'll share what's working}.\n\n{I read every reply|I read all of these myself}.\n\nBest"),
    (8,
     "{business_name}",
     "Hi {business_name} team,\n\n{Still thinking about {business_name}|{business_name} is still on my mind} - {is new client flow where you want it right now?|are you happy with how new clients find you?}\n\n{Just reply with a yes or no|One-line answer is fine}.\n\nBest"),
    (9,
     "Last one, {business_name}",
     "Hi {business_name} team,\n\n{This is genuinely my last note|Last one from me, I mean it}. {If there's a fit, it's a 10-minute chat|If it's ever relevant, it's a 10-minute conversation}.\n\n{If not, all good|Otherwise, wishing you well}.\n\nBest"),
    (10,
     "Closing the file on {business_name}",
     "Hi {business_name} team,\n\n{Closing your file on my end|I'm closing the loop for good}. {Door's open if things change|You know where to find me if timing changes}.\n\nBest"),
]

_DEFAULT_BY_STEP = {s: (subj, body) for s, subj, body in DEFAULT_FOLLOWUPS}


def default_for(step):
    """(subject, body) defaults for a step number, or ("", "") if unknown."""
    return _DEFAULT_BY_STEP.get(step, ("", ""))


def seed_defaults(campaign_id, count=DEFAULT_COUNT):
    """Insert default follow-ups for steps 1..count (only missing steps)."""
    import time
    for step, subj, body in DEFAULT_FOLLOWUPS:
        if step > count:
            break
        existing = db.q("SELECT id FROM followups WHERE campaign_id=? AND step=?",
                        (campaign_id, step), one=True)
        if not existing:
            db.w("INSERT INTO followups (campaign_id, step, subject_tpl, body_tpl, created_at)"
                 " VALUES (?,?,?,?,?)", (campaign_id, step, subj, body, time.time()))


def set_count(campaign_id, count):
    """Set how many steps this campaign uses: drop rows beyond count,
    seed defaults for any missing steps within count. Returns the count."""
    count = max(1, min(MAX_STEPS, int(count or DEFAULT_COUNT)))
    db.w("DELETE FROM followups WHERE campaign_id=? AND step>?",
         (campaign_id, count))
    seed_defaults(campaign_id, count)
    return count


def get_followups(campaign_id, count=None):
    rows = db.q("SELECT * FROM followups WHERE campaign_id=? ORDER BY step",
                (campaign_id,))
    if count:
        rows = [r for r in rows if r["step"] <= count]
    return rows


def get_followup(campaign_id, step):
    return db.q("SELECT * FROM followups WHERE campaign_id=? AND step=?",
                (campaign_id, step), one=True)


def upsert_followup(campaign_id, step, subject_tpl, body_tpl):
    """Save a step. A blank body disables the step (row removed)."""
    import time
    body = (body_tpl or "").strip()
    if not body:
        db.w("DELETE FROM followups WHERE campaign_id=? AND step=?",
             (campaign_id, step))
        return
    existing = get_followup(campaign_id, step)
    if existing:
        db.w("UPDATE followups SET subject_tpl=?, body_tpl=? WHERE id=?",
             (subject_tpl or "", body, existing["id"]))
    else:
        db.w("INSERT INTO followups (campaign_id, step, subject_tpl, body_tpl, created_at)"
             " VALUES (?,?,?,?,?)",
             (campaign_id, step, subject_tpl or "", body, time.time()))


def is_enabled(camp):
    """Follow-ups enabled for this campaign? NULL (pre-migration) counts as on."""
    v = (camp or {}).get("followups_enabled")
    return True if v is None else bool(v)


def step_count(camp):
    """Number of follow-up steps configured (1..MAX_STEPS)."""
    try:
        n = int((camp or {}).get("followup_count") or DEFAULT_COUNT)
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    return max(1, min(MAX_STEPS, n))


def delay_hours(camp):
    try:
        h = int((camp or {}).get("followup_delay_hours") or DEFAULT_DELAY_HOURS)
    except (TypeError, ValueError):
        h = DEFAULT_DELAY_HOURS
    return max(1, min(720, h))
