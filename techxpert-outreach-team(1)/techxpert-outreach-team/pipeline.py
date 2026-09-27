"""Full Autopilot pipeline: niche + location in, outreach out.

One campaign mode where the app does everything by itself on scheduler
ticks. Each tick advances ONE stage for each pipeline-enabled campaign:

    discover -> enrich -> validate -> write -> queue -> done

  discover : web search for "{niche} {location}" until the target lead
             count is reached (a few new business domains per tick).
  enrich   : visit lead websites and extract publicly listed emails.
             Leads with no public email are marked and skipped, never
             retried, never mailed.
  validate : SMTP/MX-validate found emails. Invalid addresses are dropped
             (email cleared). Only valid/risky addresses move on.
  write    : the Autopilot AI writer drafts one personal email + 3
             follow-ups per validated lead (skipped gracefully when no
             API key is set; normal templates are used instead).
  queue    : validated, selected, never-contacted leads enter the normal
             send queue. From here the existing sender takes over, with
             all the usual guards: reply stops the sequence, a bounce
             stops the sequence, caps and windows are respected.

Per-tick limits are small on purpose (never more than ~10 leads per
stage per tick) so a serverless tick stays fast and nothing hammers
anyone. Never guesses emails: only publicly found, validated addresses
enter the send queue.
"""
import json
import logging
import re
import time

import db

log = logging.getLogger(__name__)

DISCOVER_PER_TICK = 5   # new business domains identified per tick
ENRICH_PER_TICK = 3     # websites crawled for emails per tick
VALIDATE_PER_TICK = 10  # emails validated per tick (concurrent probes)
WRITE_PER_TICK = 1      # AI emails written per tick (slow: site fetch + AI)
QUEUE_PER_TICK = 25     # queue inserts are cheap DB writes
TICK_BUDGET_S = 35      # stop advancing campaigns past this per tick

STAGES = ("discover", "enrich", "validate", "write", "queue", "done")

STAGE_LABELS = {
    "discover": "Finding leads",
    "enrich": "Finding emails",
    "validate": "Validating emails",
    "write": "Writing AI emails",
    "queue": "Queueing sends",
    "done": "Done",
}

# Query variants rotate across ticks so discovery keeps finding NEW
# domains instead of re-adding the same top results.
QUERY_VARIANTS = [
    "{niche} {location}",
    "{niche} in {location}",
    "best {niche} {location}",
    "{location} {niche}",
]

# Verdicts allowed into the send queue. "risky" (catch-all server) is
# acceptable for cold outreach; "unknown" is held back, "invalid" and
# "none" never mail.
SENDABLE_VERDICTS = ("valid", "risky")


# ---------------------------------------------------------------------------
# Discovery filter: keep real businesses, drop aggregators/listicles/junk.
# Shared by the Full Autopilot pipeline (_discover) and the manual
# web-search job (jobs._websearch_chunk).
# ---------------------------------------------------------------------------

# Domains that never belong to a single real business: aggregators,
# directories, review sites, social networks, search engines. Easy to
# extend: just add the site's base domain, e.g. "sometravelsite.com".
# Matching is by domain part, so "www.tripadvisor.de" and "m.yelp.com"
# are caught too.
AGGREGATOR_DOMAINS = (
    "tripadvisor.com", "yelp.com", "google.com", "facebook.com",
    "instagram.com", "linkedin.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "foursquare.com",
    "groupon.com", "trustpilot.com", "angi.com", "thumbtack.com",
    "homeadvisor.com", "yellowpages.com", "bbb.org", "mapquest.com",
    "manta.com", "superpages.com", "dexknows.com", "houzz.com",
    "porch.com", "expertise.com", "consumeraffairs.com",
    "chamberofcommerce.com", "wikipedia.org", "local.yahoo.com",
    "reddit.com", "pinterest.com", "mysanantonio.com", "forbes.com",
    "bobvila.com", "thisoldhouse.com", "familyhandyman.com",
    "duckduckgo.com",
)

# Brands matched as a domain part (catches country TLDs like
# tripadvisor.de). Short brands (<=2 chars, e.g. "x") are excluded here
# so innocent domains like box.com never match; they are still caught by
# the exact/suffix check in is_aggregator_domain.
_AGGREGATOR_BRANDS = frozenset(
    p.split(".")[0] for p in AGGREGATOR_DOMAINS if len(p.split(".")[0]) > 2
)


def is_aggregator_domain(netloc):
    """True when the domain is an aggregator/directory/social/search site."""
    d = (netloc or "").lower().strip()
    if not d:
        return True
    host = d.split(":")[0].lstrip(".")
    parts = host.split(".")
    if any(part in _AGGREGATOR_BRANDS for part in parts):
        return True
    return any(host == p or host.endswith("." + p)
               for p in AGGREGATOR_DOMAINS)


# Titles that read like a ranking article or guide rather than one
# business homepage.
LISTICLE_PATTERNS = (
    r"\b\d+\s+(best|top)\b",         # "11 Top Locations", "10 Best Gyms"
    r"\b(best|top)\s+\d+\b",         # "Best 10", "Top 11"
    r"\bbest\b.{0,60}\bin\b",        # "Best Gym in Berlin"
    r"\btop\b.{0,60}\bin\b",         # "Top Gyms in Berlin"
    r"\blocations?\s+for\s+your\b",  # "Locations for Your Training"
    r"\b(ultimate\s+)?guide\b",      # "guide", "Ultimate Guide"
    r"\brankings?\b",
    r"\breviews?\b",
)
LISTICLE_RES = tuple(re.compile(p, re.I) for p in LISTICLE_PATTERNS)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _is_generic_name(name, niche, location):
    """True for placeholder names like "Berlin10" or "Berlin Gyms and Fitness"."""
    n = (name or "").strip().lower()
    if not n:
        return False
    # "<city><number>", e.g. Berlin10
    m = re.match(r"^([^\W\d_]+)\s*(\d{1,4})$", n, re.UNICODE)
    if m:
        loc_words = set(_WORD_RE.findall((location or "").lower()))
        if m.group(1) in loc_words:
            return True
    # name made only of niche/location words, e.g. "Berlin Gyms and Fitness"
    qwords = set(_WORD_RE.findall(f"{niche or ''} {location or ''}".lower()))
    nwords = set(_WORD_RE.findall(n))
    return bool(nwords) and nwords <= qwords


def discovery_verdict(name, url, niche="", location=""):
    """Keep or drop one discovery result.

    Returns (keep, reason): reason is "" when the result is kept, else a
    short explanation that is written to the logs so drops stay debuggable.
    """
    from urllib.parse import urlparse
    dom = urlparse(url or "").netloc.lower()
    if not dom:
        return False, "no domain in URL"
    if is_aggregator_domain(dom):
        return False, f"aggregator/directory/social domain ({dom})"
    title = (name or "").strip()
    if title:
        for rx in LISTICLE_RES:
            if rx.search(title):
                return False, f"listicle/guide title ({title[:60]})"
        if _is_generic_name(title, niche, location):
            return False, f"generic name ({title[:60]})"
    return True, ""


def log_skipped(url, name, reason):
    """One log line per dropped discovery result (visible in server logs)."""
    log.info("discovery skipped: %s | name=%r | reason=%s",
             url, (name or "")[:60], reason)


def _cursor(camp):
    try:
        return json.loads(camp.get("pipeline_cursor") or "{}")
    except Exception:
        return {}


def _save_cursor(cid, cursor):
    db.w("UPDATE campaigns SET pipeline_cursor=? WHERE id=?",
         (json.dumps(cursor), cid))


def _set_stage(cid, stage):
    db.w("UPDATE campaigns SET pipeline_stage=? WHERE id=?", (stage, cid))


def _lead_count(cid):
    row = db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=?", (cid,), one=True)
    return (row["c"] if row else 0) or 0


# ---------------- discover ----------------

def _discover(camp):
    """Add a few new leads from web search. Returns next stage or None."""
    from scrapers import websearch
    from urllib.parse import urlparse
    cid = camp["id"]
    niche = (camp.get("niche") or "").strip()
    location = (camp.get("location") or "").strip()
    if not niche or not location:
        return None  # misconfigured; wait for the user to fill niche/location
    target = camp.get("pipeline_target_leads") or 50
    if _lead_count(cid) >= target:
        return "enrich"
    cur = _cursor(camp)
    start_variant = int(cur.get("v", 0) or 0) % len(QUERY_VARIANTS)
    existing = db.q("SELECT website FROM leads WHERE campaign_id=?", (cid,))
    seen_domains = set()
    for r in existing:
        try:
            seen_domains.add(urlparse(r["website"] or "").netloc.lower())
        except Exception:
            pass
    new_urls = []
    query_used = ""
    for i in range(len(QUERY_VARIANTS)):
        variant = (start_variant + i) % len(QUERY_VARIANTS)
        query = QUERY_VARIANTS[variant].format(niche=niche, location=location)
        query_used = query
        try:
            links = websearch.ddg_links(query)
        except Exception:
            links = []
        if not links:
            try:
                time.sleep(5)
                links = websearch.ddg_links(query)
            except Exception:
                links = []
        for u in links:
            try:
                dom = urlparse(u).netloc.lower()
            except Exception:
                continue
            if not dom or dom in seen_domains or dom in [d for d, _ in new_urls]:
                continue
            if is_aggregator_domain(dom):
                log_skipped(u, "", f"aggregator/directory/social domain ({dom})")
                seen_domains.add(dom)
                continue
            new_urls.append((dom, u))
        if new_urls:
            cur["v"] = variant  # stay on this variant until it is exhausted
            break
        cur["v"] = (variant + 1) % len(QUERY_VARIANTS)
    _save_cursor(cid, cur)
    if not new_urls:
        # All variants exhausted: move on with what we have.
        db.w("UPDATE campaigns SET pipeline_cursor=? WHERE id=?",
             (json.dumps(dict(cur, exhausted=query_used)), cid))
        return "enrich"
    added = 0
    for dom, url in new_urls[:DISCOVER_PER_TICK]:
        try:
            ident = websearch._site_identity(url)
        except Exception:
            ident = {}
        name = ((ident or {}).get("name") or "").strip()
        if name and websearch.ARTICLE_TITLE_RE.search(name):
            log_skipped(url, name, "article/ranking title (ARTICLE_TITLE_RE)")
            seen_domains.add(dom)
            continue
        keep, reason = discovery_verdict(name, url, niche, location)
        if not keep:
            log_skipped(url, name, reason)
            seen_domains.add(dom)
            continue
        if not name:
            name = dom.replace("www.", "")
        site = f"{urlparse(url).scheme}://{dom}"
        dup = db.q("SELECT id FROM leads WHERE campaign_id=? AND website=?",
                   (cid, site), one=True)
        if not dup:
            db.w(
                """INSERT INTO leads (user_id, campaign_id, business_name, address, phone,
                   website, email, rating, review_count, category, source, notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (camp["user_id"], cid, name, (ident or {}).get("address", ""),
                 (ident or {}).get("phone", ""), site, "", "", "",
                 niche, "pipeline", f"pipeline discovery: {query_used}", time.time()))
            added += 1
        seen_domains.add(dom)
    if _lead_count(cid) >= target:
        return "enrich"
    return None


# ---------------- enrich ----------------

def _enrich(camp):
    """Extract public emails for a few leads. Returns next stage or None."""
    import enrich as enrichmod
    cid = camp["id"]
    rows = db.q(
        "SELECT id, website FROM leads WHERE campaign_id=? AND website<>'' "
        "AND email='' AND COALESCE(email_verdict,'')='' ORDER BY id LIMIT ?",
        (cid, ENRICH_PER_TICK))
    if not rows:
        return "validate"
    results = enrichmod.enrich_leads(rows, pause=0.8)
    for res in results:
        if res["email"]:
            # Found a public address; validation happens in the next stage.
            db.w("""UPDATE leads SET email=?, email_verdict='',
                    phone=COALESCE(NULLIF(phone,''), ?),
                    has_contact_form=?, notes=? WHERE id=?""",
                 (res["email"], res["phone"] or "",
                  1 if res["has_contact_form"] else 0,
                  f"pipeline: public email found ({res['pages_checked']} pages)",
                  res["lead_id"]))
        else:
            # No public email: listed, skipped silently from here on.
            db.w("""UPDATE leads SET email_verdict='none', has_contact_form=?,
                    notes='pipeline: no public email found' WHERE id=?""",
                 (1 if res["has_contact_form"] else 0, res["lead_id"]))
    return None


# ---------------- validate ----------------

def _validate(camp):
    """Validate found emails. Drops invalid ones. Returns next stage or None."""
    from email_validator import validate_emails
    cid = camp["id"]
    rows = db.q(
        "SELECT id, email FROM leads WHERE campaign_id=? AND email<>'' "
        "AND COALESCE(email_verdict,'')='' ORDER BY id LIMIT ?",
        (cid, VALIDATE_PER_TICK))
    if not rows:
        return "write"
    results = validate_emails([r["email"] for r in rows], max_workers=5)
    for row, res in zip(rows, results):
        verdict = res["verdict"]
        detail = res["reason"]
        if res["mx_host"]:
            detail += f" [{res['mx_host']}]"
        if verdict == "invalid":
            # Dropped: email cleared so it can never enter the send queue.
            db.w("UPDATE leads SET email='', email_verdict='invalid', "
                 "notes=? WHERE id=?",
                 (f"pipeline: email dropped ({detail[:150]})", row["id"]))
        else:
            db.w("UPDATE leads SET email_verdict=?, email_verdict_detail=? WHERE id=?",
                 (verdict, detail[:200], row["id"]))
    return None


# ---------------- write ----------------

def _write(camp):
    """AI-write emails for validated leads. Returns next stage or None."""
    import config
    import ai_writer as aimod
    cid = camp["id"]
    if not config.ai_enabled():
        # No API key: skip AI writing; the normal templates send instead.
        return "queue"
    rows = db.q(
        "SELECT * FROM leads WHERE campaign_id=? AND selected=1 AND email<>'' "
        "AND email_verdict IN ('valid','risky') "
        "AND (ai_status IS NULL OR ai_status='') ORDER BY id LIMIT ?",
        (cid, WRITE_PER_TICK))
    if not rows:
        return "queue"
    for lead in rows:
        try:
            aimod.generate_and_store(lead, camp)
        except Exception:
            db.w("UPDATE leads SET ai_status='error', ai_note=? WHERE id=?",
                 ("AI writing failed; the normal template will be used.", lead["id"]))
    return None


# ---------------- queue ----------------

def _queue(camp):
    """Queue validated, never-contacted leads into the normal send flow."""
    cid = camp["id"]
    rows = db.q(
        """SELECT l.* FROM leads l WHERE l.campaign_id=? AND l.selected=1
           AND l.email<>'' AND l.email_verdict IN ('valid','risky') AND l.replied=0
           AND NOT EXISTS (SELECT 1 FROM send_queue q
                           WHERE q.campaign_id=l.campaign_id AND q.lead_id=l.id)
           AND NOT EXISTS (SELECT 1 FROM send_log s
                           WHERE s.campaign_id=l.campaign_id AND s.lead_id=l.id
                           AND s.status IN ('sent','dry-run'))
           AND NOT EXISTS (SELECT 1 FROM send_log b
                           WHERE b.campaign_id=l.campaign_id AND b.lead_id=l.id
                           AND b.status='bounced')
           ORDER BY l.id LIMIT ?""",
        (cid, QUEUE_PER_TICK))
    now = time.time()
    for lead in rows:
        db.w("INSERT INTO send_queue (campaign_id, lead_id, status, scheduled_at, step)"
             " VALUES (?,?, 'pending', ?, 0)",
             (cid, lead["id"], now))
    if rows:
        db.w("UPDATE campaigns SET status='sending', next_send_at=? WHERE id=?",
             (now, cid))
        return None
    # Nothing left to queue: pipeline work is done. Sending, follow-ups,
    # reply scans and bounce guards continue through the normal flow.
    return "done"


_STAGE_FNS = {
    "discover": _discover,
    "enrich": _enrich,
    "validate": _validate,
    "write": _write,
    "queue": _queue,
}


def tick_campaign(camp):
    """Advance one pipeline stage for one campaign. Returns (stage, note)."""
    stage = (camp.get("pipeline_stage") or "discover").strip() or "discover"
    if stage == "done":
        return "done", ""  # terminal: sending/follow-ups continue via normal flow
    if stage not in _STAGE_FNS:
        stage = "discover"
    note = ""
    try:
        nxt = _STAGE_FNS[stage](camp)
    except Exception as e:
        return stage, f"pipeline stage '{stage}' error: {str(e)[:150]}"
    if nxt and nxt != stage:
        _set_stage(camp["id"], nxt)
        return nxt, f"advanced to {STAGE_LABELS.get(nxt, nxt)}"
    return stage, note


def tick_all():
    """One scheduler tick: advance one stage per pipeline-enabled campaign.

    Respects a time budget so the rest of the tick (sends) keeps its room.
    Returns a summary dict.
    """
    start = time.time()
    summary = {"campaigns": [], "advanced": 0}
    camps = db.q("SELECT * FROM campaigns WHERE pipeline_enabled=1 ORDER BY id")
    for camp in camps:
        if time.time() - start > TICK_BUDGET_S:
            summary["budget_hit"] = True
            break
        stage, note = tick_campaign(camp)
        summary["campaigns"].append(
            {"id": camp["id"], "name": camp["name"], "stage": stage, "note": note})
        if note.startswith("advanced"):
            summary["advanced"] += 1
    summary["elapsed_s"] = round(time.time() - start, 1)
    return summary


def stats(cid):
    """Pipeline status numbers for the campaign page."""
    one = lambda sql, args=(): ((db.q(sql, args, one=True) or {}).get("c")) or 0
    found = one("SELECT COUNT(*) c FROM leads WHERE campaign_id=?", (cid,))
    emails = one("SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND email<>''", (cid,))
    validated = one(
        "SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND email_verdict IN ('valid','risky')",
        (cid,))
    sent = one(
        "SELECT COUNT(DISTINCT lead_id) c FROM send_log WHERE campaign_id=? "
        "AND status IN ('sent','dry-run')", (cid,))
    replies = one("SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND replied=1", (cid,))
    queued = one("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=? AND status='pending'",
                 (cid,))
    return {"found": found, "emails": emails, "validated": validated,
            "sent": sent, "replies": replies, "queued": queued}
