"""Full Autopilot pipeline: niche + location in, outreach out.

One campaign mode where the app does everything by itself on scheduler
ticks. Each tick advances ONE stage for each pipeline-enabled campaign:

    discover -> enrich -> people -> validate -> write -> queue -> done

  discover : OpenStreetMap business data first (real, mapped businesses:
             Nominatim geocoding + Overpass POIs, no API key, global),
             then rotating localized web-search queries built from niche +
             location (local-language variants, e.g. German for Germany,
             French for France, plus district-by-district queries for
             mapped cities). Each tick runs several query variants
             (a few OSM tag groups, then web queries) until the per-tick
             cap is hit, so a campaign discovers dozens of new leads per
             tick. Discovery pauses each day once the campaign's
             daily_discovery_target new leads are in, and resumes the next
             day: a finished pipeline ("done") goes back to "discover" at
             the start of a new day, so fresh leads keep flowing in a
             continuous growth loop while sending continues through the
             normal queue with all the usual guards.
  enrich   : visit lead websites and extract publicly listed emails.
             Leads with no public email are marked and skipped, never
             retried, never mailed.
  people   : decision-maker enrichment (decision_makers.py): ask Gemini with
             web-search grounding who runs each business, then corroborate
             every suggestion against the business's own site/imprint or 2+
             independent sources. Only verified people are stored, with only
             publicly listed emails attached (never guessed). Skipped
             gracefully when no Gemini API key is set.
  validate : SMTP/MX-validate found emails. Invalid addresses are dropped
             (email cleared). Only valid/risky addresses move on.
  write    : the Autopilot AI writer drafts one personal email + 3
             follow-ups per validated lead (skipped gracefully when no
             API key is set; normal templates are used instead).
  queue    : validated, selected, never-contacted leads enter the normal
             send queue. From here the existing sender takes over, with
             all the usual guards: reply stops the sequence, a bounce
             stops the sequence, caps and windows are respected.

Per-tick limits: discovery is the volume stage (dozens of new leads per
tick across several query variants); the later stages are sized so a full
100-lead daily batch clears within a day of 15-minute ticks
(enrich 3/tick -> ~290/day, validate 10/tick -> ~960/day,
write 3/tick -> ~290/day, queue 25/tick). Never guesses emails: only
publicly found, validated addresses enter the send queue.
"""
import json
import logging
import re
import time
from datetime import datetime

import db
import geo
import lead_quality as lq

log = logging.getLogger(__name__)

DISCOVER_PER_TICK = 30   # new leads inserted per tick (across all query variants)
OSM_GROUPS_PER_TICK = 3  # Overpass tag groups per tick, sequential, never parallel
WEB_QUERIES_PER_TICK = 2  # web-search queries per tick
DISCOVER_TIME_BUDGET_S = 25  # stop starting new discovery queries past this
ENRICH_PER_TICK = 8      # websites crawled for emails per tick
DM_PER_TICK = 2         # businesses searched for decision makers per tick
                        # (each does site fetches + one grounded AI call)
DM_VALIDATE_PER_TICK = 5  # decision-maker emails validated per tick
VALIDATE_PER_TICK = 10  # emails validated per tick (concurrent probes)
WRITE_PER_TICK = 3      # AI emails written per tick (slow: site fetch + AI)
QUEUE_PER_TICK = 25     # queue inserts are cheap DB writes
TICK_BUDGET_S = 35      # stop advancing campaigns past this per tick

DAILY_DISCOVERY_DEFAULT = 100  # new leads per day per campaign when unset

STAGES = ("discover", "enrich", "people", "validate", "write", "queue", "done")

STAGE_LABELS = {
    "discover": "Finding leads",
    "enrich": "Finding emails",
    "people": "Finding decision makers",
    "validate": "Validating emails",
    "write": "Writing AI emails",
    "queue": "Queueing sends",
    "done": "Done",
}

# Rotating, localized discovery queries. Built per campaign from niche +
# location so each scheduler tick searches a fresh angle instead of
# repeating one generic query. The rotation index lives in the campaign's
# pipeline_cursor ("q") so it survives restarts.
# Niche translations live in geo.py (multilingual: de/fr/es/pt/it/nl,
# word-boundary, longest-first, case-insensitive; new languages are
# data-only additions). pipeline keeps thin wrappers below so callers
# and tests are unaffected.

# Neighborhood/district queries per city (normalized city name -> districts).
# Lets discovery sweep a metro area district by district. Add more cities
# here as campaigns expand; cities without an entry fall back to the plain
# location query.
LOCATION_DISTRICTS = {
    "berlin": ("Mitte", "Kreuzberg", "Charlottenburg", "Prenzlauer Berg",
               "Friedrichshain", "Schoneberg", "Neukolln", "Wedding",
               "Moabit", "Pankow", "Reinickendorf", "Spandau"),
}

BASE_QUERY_TEMPLATES = ("{n} {loc}", "{n} in {loc}", "{loc} {n}")


def _location_lang(location):
    """ISO 639-1 language for the location; 'en' fallback. See geo.py."""
    lang = geo.location_language(location)
    if lang == "en":
        # District-mapped cities keep their previous behavior (Berlin is
        # German even when typed without a country).
        city = geo.parse_location(location)[0].lower()
        if city in LOCATION_DISTRICTS:
            return "de"
    return lang


def _translate_niche(niche, lang):
    """Translate common niche words; falls back to the English niche."""
    return geo.translate_niche(niche, lang)


def build_discovery_queries(niche, location):
    """Ordered, deduplicated search queries for one campaign.

    For non-English locations (e.g. Berlin, Germany) the localized
    queries come first: translated base templates, then translated
    district queries, then the English base templates, then the
    English district queries. The scheduler rotates one query per
    tick, and for German locations the English queries mostly return
    aggregators/listicles that get filtered out (0 new leads), so the
    German queries must hit first. English locations keep the plain
    English order. Pure function: easy to test, no DB.
    """
    niche = (niche or "").strip()
    location = (location or "").strip()
    if not niche or not location:
        return []
    lang = _location_lang(location)
    translated = ""
    if lang != "en":
        translated = _translate_niche(niche, lang)
        if translated.lower() == niche.lower():
            translated = ""
    en_base = [t.format(n=niche, loc=location) for t in BASE_QUERY_TEMPLATES]
    de_base = ([t.format(n=translated, loc=location) for t in BASE_QUERY_TEMPLATES]
               if translated else [])
    city = location.split(",")[0].strip()
    districts = LOCATION_DISTRICTS.get(city.lower(), ())
    en_dist = [f"{niche} {district} {city}" for district in districts]
    de_dist = ([f"{translated} {district} {city}" for district in districts]
               if translated else [])
    queries = (de_base + de_dist + en_base + en_dist) if translated else (en_base + en_dist)
    seen, out = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def pick_query(queries, cursor):
    """This tick's query plus the next rotation index. Pure rotation."""
    idx = int((cursor or {}).get("q", 0) or 0) % len(queries)
    return queries[idx], (idx + 1) % len(queries)

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
    # German review/directory/comparison portals (seen returning junk in a
    # real Berlin shakedown run): review aggregator, city directory,
    # listicle site, gym comparison portal.
    "werkenntdenbesten.de", "citiesinsider.com", "besteberlin.com",
    "gymfind.de", "unilocal.de", "cylex.de",
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
# business homepage. English patterns first, then German equivalents:
# "Die 10 besten Fitnessstudios", "Beste ... in Berlin", "im Vergleich".
LISTICLE_PATTERNS = (
    r"\b\d+\s+(best|top)\b",         # "11 Top Locations", "10 Best Gyms"
    r"\b(best|top)\s+\d+\b",         # "Best 10", "Top 11"
    r"\bbest\b.{0,60}\bin\b",        # "Best Gym in Berlin"
    r"\btop\b.{0,60}\bin\b",         # "Top Gyms in Berlin"
    r"\blocations?\s+for\s+your\b",  # "Locations for Your Training"
    r"\b(ultimate\s+)?guide\b",      # "guide", "Ultimate Guide"
    r"\brankings?\b",
    r"\breviews?\b",
    r"\b\d+\s+besten\b",             # "Die 10 besten Fitnessstudios"
    r"\bbeste[nsr]?\b.{0,60}\bin\b", # "Beste Fitnessstudios in Berlin"
    r"\bim vergleich\b",             # "59 Studios im Vergleich"
    r"\bvergleich\b",                # "Vergleich" (comparison)
    r"\bdie\s+besten\b",             # "Die Besten der Stadt",
                                     # "... Die besten Fitnessstudios von Günstig bis Premium"
    r"\b\d+\s*x\s+in\b",             # "EVO Fitness 3x in Berlin: ..." (listicle title)
    r"\bpreise\s+und\s+bewertungen\b",  # "... Öffnungszeiten, Preise und Bewertungen"
)
LISTICLE_RES = tuple(re.compile(p, re.I) for p in LISTICLE_PATTERNS)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Words that carry no business identity on their own. Stopwords (English +
# German, since discovery queries can be German) plus industry-generic
# filler: a name that says nothing beyond the niche + location is a
# placeholder, e.g. "Berlin Gyms and Fitness" or "Berlin Fitness Club".
_FILLER_WORDS = frozenset(
    "and und the der die das of von in im for fuer für &".split())
_INDUSTRY_FILLER_WORDS = frozenset(
    "fitness fitnessstudio fitnessstudios gym gyms studio studios "
    "center centre centers centres club clubs".split())


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
    # name made only of niche/location words (plus filler words), e.g.
    # "Berlin Gyms and Fitness". German niche variants count too, so
    # "Berlin Fitnessstudios" is caught the same way.
    niche_words = set(_WORD_RE.findall((niche or "").lower()))
    niche_words |= set(_WORD_RE.findall(
        _translate_niche(niche or "", _location_lang(location)).lower()))
    qwords = (set(_WORD_RE.findall((location or "").lower()))
              | niche_words | _INDUSTRY_FILLER_WORDS)
    nwords = set(_WORD_RE.findall(n)) - _FILLER_WORDS
    return bool(nwords) and nwords <= qwords


def discovery_verdict(name, url, niche="", location="", allow_no_url=False):
    """Keep or drop one discovery result.

    Returns (keep, reason): reason is "" when the result is kept, else a
    short explanation that is written to the logs so drops stay debuggable.
    When allow_no_url is True (OSM results), a missing URL skips the
    domain checks but the name checks (listicle / generic-name) still run,
    so real named businesses are kept instead of being junked.
    """
    from urllib.parse import urlparse
    dom = urlparse(url or "").netloc.lower()
    if not dom and not allow_no_url:
        return False, "no domain in URL"
    if dom and is_aggregator_domain(dom):
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


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _daily_target(camp):
    """New leads per day for this campaign (default 100)."""
    try:
        return max(1, int(camp.get("daily_discovery_target") or DAILY_DISCOVERY_DEFAULT))
    except (TypeError, ValueError):
        return DAILY_DISCOVERY_DEFAULT


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

def _try_insert_lead(camp, name, site, address, phone, niche, notes,
                     source, allow_no_url=False):
    """Junk-filter, dedup, then insert one discovery lead.

    Returns True only when a NEW lead row was inserted (dedup-aware:
    duplicates merge into the existing row and junk is dropped, both
    returning False). Used by both the OSM and web discovery paths so the
    daily counter only counts genuinely new leads.
    """
    cid = camp["id"]
    location = camp.get("location") or ""
    keep, reason = discovery_verdict(name, site, niche, location,
                                     allow_no_url=allow_no_url)
    if not keep:
        log_skipped(site or "(no website)", name, reason)
        return False
    dup = lq.find_duplicate_lead(cid, name, site)
    if dup:
        # Same business found twice (e.g. "McFIT Berlin Mitte" vs
        # "mcfit berlin mitte gmbh"): merge new fields into the existing
        # row instead of creating a duplicate lead.
        if lq.merge_lead_fields(dup["id"], name=name, address=address,
                                phone=phone, website=site):
            log.info("discovery merged into lead %s: %s", dup["id"], name)
        return False
    lid = db.w(
        """INSERT INTO leads (user_id, campaign_id, business_name, address, phone,
           website, email, rating, review_count, category, source, notes, fit, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (camp["user_id"], cid, name, address or "", phone or "",
         site, "", "", "", niche, source, notes,
         lq.detect_fit(name, niche), time.time()))
    import crm as _crm
    _crm.log_event(lid, "lead_created",
                   f"Lead discovered: {name}" + (f" ({site})" if site else ""),
                   meta={"source": source})
    return True


def _discover_osm(camp, niche, location, cur, budget, deadline):
    """Up to OSM_GROUPS_PER_TICK tag groups this tick (real, mapped
    businesses, no API key). Overpass calls are sequential with a 1s
    politeness gap between them, never parallel; each call has a timeout
    plus one retry. Returns the number of NEW leads inserted (stops at
    `budget` or `deadline`). 0 means OSM is exhausted, unmapped for this
    niche, or failed this tick -> the caller still runs web search.
    OSM failures never stall the campaign.
    Research: docs/discovery-research.md.
    """
    import osm_discovery as osm
    cid = camp["id"]
    ostate = cur.get("osm") or {}
    if ostate.get("exhausted"):
        return 0
    if osm.niche_tag_groups(niche) is None:
        # Researched as unmapped in OSM (e.g. trades): skip the geocode
        # entirely, web search covers this niche.
        ostate["exhausted"] = True
        cur["osm"] = ostate
        _save_cursor(cid, cur)
        return 0
    lat, lon = ostate.get("lat"), ostate.get("lon")
    if lat is None or lon is None:
        try:
            lat, lon = osm.geocode(location)  # once per campaign; cached below
        except Exception as e:
            log.warning("osm: geocoding failed for %r: %s", location, str(e)[:120])
            return 0
        if lat is None:
            log.warning("osm: geocoding failed for %r; web search covers this tick", location)
            return 0
        ostate["lat"], ostate["lon"] = lat, lon
        cur["osm"] = ostate
        _save_cursor(cid, cur)
    added = 0
    for g in range(OSM_GROUPS_PER_TICK):
        if added >= budget or time.time() > deadline:
            break
        if g:
            time.sleep(1.0)  # OSM fair-use: politeness gap between calls
        try:
            leads, nxt, exhausted = osm.discover(
                niche, location, lat=lat, lon=lon,
                group_index=int(ostate.get("gi") or 0))
        except Exception as e:
            log.warning("osm discover failed: %s", str(e)[:120])
            break
        ostate["gi"] = nxt
        if exhausted:
            ostate["exhausted"] = True
        for lead in leads:
            if added >= budget:
                break
            name = (lead.get("business_name") or "").strip()
            site = (lead.get("website") or "").strip()
            if _try_insert_lead(camp, name, site, lead.get("address", ""),
                                lead.get("phone", ""), niche,
                                lead.get("notes", "osm discovery"), "osm",
                                allow_no_url=True):
                added += 1
        if exhausted:
            break
    cur["osm"] = ostate
    _save_cursor(cid, cur)
    return added


def _discover_web(camp, niche, location, cur, queries, budget, deadline):
    """Up to WEB_QUERIES_PER_TICK rotated localized web-search queries this
    tick. Returns the number of NEW leads inserted (stops at `budget` or
    `deadline`). The rotation index ("q") and the consecutive-empty-tick
    counter ("empty") persist in the campaign cursor.
    """
    from scrapers import websearch
    from urllib.parse import urlparse
    cid = camp["id"]
    existing = db.q("SELECT website FROM leads WHERE campaign_id=?", (cid,))
    seen_domains = set()
    for r in existing:
        try:
            seen_domains.add(urlparse(r["website"] or "").netloc.lower())
        except Exception:
            pass
    added = 0
    for _ in range(WEB_QUERIES_PER_TICK):
        if added >= budget or time.time() > deadline:
            break
        query, cur["q"] = pick_query(queries, cur)
        log.info("discovery tick: campaign %s query %d/%d: %r",
                 cid, (cur["q"] - 1) % len(queries) + 1, len(queries), query)
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
        new_urls = []
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
        had_new = False
        for dom, url in new_urls:
            if added >= budget or time.time() > deadline:
                break
            try:
                ident = websearch._site_identity(url)
            except Exception:
                ident = {}
            name = ((ident or {}).get("name") or "").strip()
            if name and websearch.ARTICLE_TITLE_RE.search(name):
                log_skipped(url, name, "article/ranking title (ARTICLE_TITLE_RE)")
                seen_domains.add(dom)
                continue
            if not name:
                name = dom.replace("www.", "")
            site = f"{urlparse(url).scheme}://{dom}"
            if _try_insert_lead(camp, name, site,
                                (ident or {}).get("address", ""),
                                (ident or {}).get("phone", ""), niche,
                                f"pipeline discovery: {query}", "pipeline"):
                added += 1
                had_new = True
            seen_domains.add(dom)
        _save_cursor(cid, cur)
    return added


def _discover(camp):
    """Add new leads: OSM tag groups first, then web-search queries, several
    query variants per tick until DISCOVER_PER_TICK new leads are in.

    Daily throttle: the cursor tracks disc_date/disc_today (new leads
    inserted today, dedup-aware). Once the campaign's daily_discovery_target
    is hit, discovery pauses until the next day and the pipeline moves on
    to enrich today's batch. The old lifetime pipeline_target_leads ceiling
    no longer gates discovery: it is kept in the DB/UI as an informational
    goal only, so the daily loop never stalls permanently. After enough
    consecutive ticks with zero new leads (a full query rotation with
    nothing new), discovery is exhausted and the pipeline moves on.
    Returns next stage or None (stay).
    """
    cid = camp["id"]
    niche = (camp.get("niche") or "").strip()
    location = (camp.get("location") or "").strip()
    if not niche or not location:
        return None  # misconfigured; wait for the user to fill niche/location
    cur = _cursor(camp)
    today = _today_str()
    if cur.get("disc_date") != today:
        cur["disc_date"] = today
        cur["disc_today"] = 0
        _save_cursor(cid, cur)
    daily_target = _daily_target(camp)
    if int(cur.get("disc_today") or 0) >= daily_target:
        # Daily target hit: pause discovery until tomorrow; today's batch
        # moves on to enrichment.
        return "enrich"
    deadline = time.time() + DISCOVER_TIME_BUDGET_S
    budget = DISCOVER_PER_TICK
    added = _discover_osm(camp, niche, location, cur, budget, deadline)
    if added < budget and time.time() < deadline:
        queries = build_discovery_queries(niche, location)
        if queries:
            added += _discover_web(camp, niche, location, cur, queries,
                                   budget - added, deadline)
    cur["disc_today"] = int(cur.get("disc_today") or 0) + added
    if added:
        cur["empty"] = 0
    else:
        # Nothing new this tick. A full rotation's worth of consecutive
        # unproductive ticks means discovery is exhausted: move on.
        cur["empty"] = int(cur.get("empty", 0) or 0) + 1
    _save_cursor(cid, cur)
    if cur["disc_today"] >= daily_target:
        return "enrich"
    if cur["empty"] >= len(build_discovery_queries(niche, location)):
        cur["empty"] = 0
        _save_cursor(cid, cur)
        return "enrich"
    return None


# ---------------- enrich ----------------

def _enrich(camp):
    """Extract public emails for a few leads. Returns next stage or None."""
    import enrich as enrichmod
    cid = camp["id"]
    rows = db.q(
        "SELECT id, website, business_name, fit FROM leads WHERE campaign_id=? AND website<>'' "
        "AND email='' AND COALESCE(email_verdict,'')='' ORDER BY id LIMIT ?",
        (cid, ENRICH_PER_TICK))
    if not rows:
        return "people"  # decision-maker stage runs next (forwards to validate itself)
    results = enrichmod.enrich_leads(rows, pause=0.5)
    niche = (camp.get("niche") or "").strip()
    by_id = {r["id"]: r for r in rows}
    import crm as _crm
    for res in results:
        lead = by_id.get(res["lead_id"]) or {}
        pages = res.get("pages_checked") or 0
        # Chain/franchise check, now that the homepage text is available.
        sig = lq.detect_fit(lead.get("business_name", ""), niche,
                            res.get("page_text", ""))
        if sig:
            db.w("UPDATE leads SET fit=? WHERE id=?", (sig, res["lead_id"]))
        elif not (lead.get("fit") or ""):
            # Name and homepage checked, no chain signals found.
            db.w("UPDATE leads SET fit='independent' WHERE id=?", (res["lead_id"],))
        addr = res.get("address", "") or ""
        if res["email"]:
            # Found a public address; validation happens in the next stage.
            db.w("""UPDATE leads SET email=?, email_verdict='',
                    phone=COALESCE(NULLIF(phone,''), ?),
                    address=COALESCE(NULLIF(address,''), ?),
                    has_contact_form=?, notes=? WHERE id=?""",
                 (res["email"], res["phone"] or "", addr,
                  1 if res["has_contact_form"] else 0,
                  f"pipeline: public email found ({res['pages_checked']} pages)",
                  res["lead_id"]))
            _crm.log_event(res["lead_id"], "enriched",
                           f"Public email found: {res['email']} ({pages} pages checked)",
                           meta={"email": res["email"]})
        else:
            # No public email: listed, skipped silently from here on.
            db.w("""UPDATE leads SET email_verdict='none', has_contact_form=?,
                    address=COALESCE(NULLIF(address,''), ?),
                    notes='pipeline: no public email found' WHERE id=?""",
                 (1 if res["has_contact_form"] else 0, addr, res["lead_id"]))
            _crm.log_event(res["lead_id"], "enriched",
                           "No public email found"
                           + ("; contact form available" if res["has_contact_form"] else ""),
                           meta={"email": ""})
        # Official social links, saved whether or not an email was found.
        enrichmod.save_social(res["lead_id"], res)
    return None


# ---------------- people (decision makers) ----------------

def _dm_enabled(camp):
    """Per-campaign toggle for decision-maker enrichment (default ON)."""
    v = camp.get("dm_enabled")
    return True if v is None else bool(int(v))


def _dm_max(camp):
    """Max decision-maker contacts stored/mailed per business (default 3)."""
    try:
        return max(1, min(4, int(camp.get("dm_max_contacts") or 3)))
    except (TypeError, ValueError):
        return 3


def _people(camp):
    """Find verified decision makers for a few leads. Returns next stage
    or None. Never fails the pipeline: without a Gemini key every lead is
    marked pending and retried on later ticks (cheap no-op until the key
    appears)."""
    import decision_makers as dmm
    cid = camp["id"]
    if not _dm_enabled(camp):
        return "validate"
    rows = db.q(
        "SELECT * FROM leads WHERE campaign_id=? AND website<>'' "
        "AND COALESCE(dm_status,'') IN ('','pending') "
        "AND COALESCE(replied,0)=0 AND COALESCE(unsubscribed,0)=0 "
        "ORDER BY id LIMIT ?",
        (cid, DM_PER_TICK))
    if not rows:
        return "validate"
    import crm as _crm
    for lead in rows:
        try:
            status, added = dmm.enrich_lead(lead, camp, max_contacts=_dm_max(camp))
            if status == "done":
                _crm.log_event(
                    lead["id"], "decision_makers_found",
                    (f"{added} verified decision maker(s) found"
                     if added else "Searched; no verifiable decision makers found"),
                    meta={"added": added})
        except Exception as e:
            log.warning("people stage: lead %s failed: %s", lead["id"], str(e)[:120])
            try:
                db.w("UPDATE leads SET dm_status='pending' WHERE id=?", (lead["id"],))
            except Exception:
                pass
    return None


# ---------------- validate ----------------

# Rows needing (re-)validation: never validated, plus rows stuck at "unknown"
# by the old blocked-probe behavior (no SMTP response from any MX host, e.g.
# port 25 blocked on the server). The validator now marks those "risky".
_NEED_REVAL = ("(COALESCE(email_verdict,'')='' "
               "OR (email_verdict='unknown' "
               "AND email_verdict_detail LIKE 'probe blocked/%'))")


def _needs_revalidation(cid):
    """True if any lead email still needs (re-)validation."""
    return db.q(
        "SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND email<>'' "
        f"AND {_NEED_REVAL}",
        (cid,), one=True)["c"] > 0


def _validate(camp):
    """Validate found emails (lead emails + decision-maker contact emails).
    Drops invalid ones. Returns next stage or None."""
    from email_validator import validate_emails
    cid = camp["id"]
    # Re-validate rows never validated, plus rows stuck at unknown by the old
    # blocked-probe behavior (no SMTP response from any MX host, e.g. port 25
    # blocked on the server): the validator now marks those "risky".
    _need_reval = _NEED_REVAL
    rows = db.q(
        "SELECT id, email FROM leads WHERE campaign_id=? AND email<>'' "
        f"AND {_need_reval} ORDER BY id LIMIT ?",
        (cid, VALIDATE_PER_TICK))
    crows = db.q(
        "SELECT c.id, c.email, c.lead_id FROM contacts c "
        "WHERE c.email<>'' "
        f"AND {_need_reval.replace('email_verdict', 'c.email_verdict')} "
        "AND c.lead_id IN (SELECT id FROM leads WHERE campaign_id=?) "
        "ORDER BY c.id LIMIT ?",
        (cid, DM_VALIDATE_PER_TICK))
    if not rows and not crows:
        return "write"

    import crm as _crm

    def apply(table, idcol, row, res):
        verdict = res["verdict"]
        detail = res["reason"]
        if res["mx_host"]:
            detail += f" [{res['mx_host']}]"
        if verdict == "invalid":
            # Dropped: email cleared so it can never enter the send queue.
            db.w(f"UPDATE {table} SET email='', email_verdict='invalid' WHERE {idcol}=?",
                 (row["id"],))
            if table == "leads":
                db.w("UPDATE leads SET notes=? WHERE id=?",
                     (f"pipeline: email dropped ({detail[:150]})", row["id"]))
        else:
            db.w(f"UPDATE {table} SET email_verdict=?, email_verdict_detail=? "
                 f"WHERE {idcol}=?",
                 (verdict, detail[:200], row["id"]))
        if table == "leads":
            _crm.log_event(
                row["id"], "email_validated",
                f"Email {verdict}: {row['email'] or '(dropped)'} ({detail[:120]})",
                meta={"verdict": verdict})
        else:
            _crm.log_event(
                row.get("lead_id"), "email_validated",
                f"Decision-maker email {verdict}: {row['email'] or '(dropped)'} ({detail[:120]})",
                contact_id=row["id"], meta={"verdict": verdict})

    if rows:
        for row, res in zip(rows, validate_emails([r["email"] for r in rows],
                                                  max_workers=5)):
            apply("leads", "id", row, res)
    if crows:
        for row, res in zip(crows, validate_emails([r["email"] for r in crows],
                                                   max_workers=5)):
            apply("contacts", "id", row, res)
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
        "AND email_verdict IN " + lq.queueable_verdict_sql() + " "
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
    """Queue validated, never-contacted leads into the normal send flow.

    When decision-maker enrichment found verified, mailable contacts for a
    lead, one queue row is inserted PER CONTACT (each contact gets their own
    step-0 send and their own follow-up chain). Leads without contacts keep
    the old behavior: one row for the lead's general email."""
    import decision_makers as dmm
    cid = camp["id"]
    rows = db.q(
        ("SELECT l.* FROM leads l WHERE l.campaign_id=? AND l.selected=1 "
         "AND l.email<>'' AND l.email_verdict IN " + lq.queueable_verdict_sql() +
         " AND l.replied=0 "
         "AND l.unsubscribed=0 "
         "AND NOT EXISTS (SELECT 1 FROM send_queue q "
         "WHERE q.campaign_id=l.campaign_id AND q.lead_id=l.id) "
         "AND NOT EXISTS (SELECT 1 FROM send_log s "
         "WHERE s.campaign_id=l.campaign_id AND s.lead_id=l.id "
         "AND s.status IN ('sent','dry-run')) "
         "AND NOT EXISTS (SELECT 1 FROM send_log b "
         "WHERE b.campaign_id=l.campaign_id AND b.lead_id=l.id "
         "AND b.status='bounced') "
         "ORDER BY l.id LIMIT ?"),
        (cid, QUEUE_PER_TICK))
    now = time.time()
    dm_on = _dm_enabled(camp)
    dm_max = _dm_max(camp)
    import crm as _crm
    for lead in rows:
        contacts = (dmm.mailable_contacts(lead["id"], dm_max) if dm_on else [])
        targets = [c["id"] for c in contacts] or [None]
        for contact_id in targets:
            db.w("INSERT INTO send_queue (campaign_id, lead_id, contact_id, status, scheduled_at, step)"
                 " VALUES (?,?,?, 'pending', ?, 0)",
                 (cid, lead["id"], contact_id, now))
        if camp.get("dry_run"):
            who = (f"{len(contacts)} decision maker(s)" if contacts
                   else "the general email")
            _crm.log_event(lead["id"], "dry_run_queued",
                           f"Queued for sending to {who} (dry-run mode: no real emails go out)",
                           meta={"contacts": len(contacts)})
    if rows:
        db.w("UPDATE campaigns SET status='sending', next_send_at=? WHERE id=?",
             (now, cid))
        return None
    # Nothing left to queue: pipeline work is done. Sending, follow-ups,
    # reply scans and bounce guards continue through the normal flow.
    return "done"


def _purge_junk(camp):
    """Retroactive junk purge, run on every scheduler tick.

    The junk/listicle/aggregator filter only screens at discovery time, so
    leads that entered before a filter improvement (e.g. old listicle or
    directory rows) sit in the DB until this re-screens them. It runs every
    tick because discovery_verdict is cheap regex and new junk can arrive
    at any time; only never-contacted leads are eligible.

    Never touched, no matter what the filter says:
      - any row in send_log (sent, dry-run, bounced, failed: all history)
      - any row in send_queue (queued now or ever)
      - replied = 1
      - unsubscribed = 1 (compliance records are kept as-is)
      - empty business name (nothing meaningful to judge; the discovery
        path never creates those, only manual imports do)

    Every deletion is logged. Returns the number of leads purged.
    """
    cid = camp["id"]
    niche = (camp.get("niche") or "").strip()
    location = (camp.get("location") or "").strip()
    rows = db.q(
        """SELECT id, business_name, website, category FROM leads
           WHERE campaign_id=?
             AND COALESCE(replied,0)=0 AND COALESCE(unsubscribed,0)=0
             AND business_name IS NOT NULL AND business_name<>''
             AND NOT EXISTS (SELECT 1 FROM send_log WHERE lead_id=leads.id)
             AND NOT EXISTS (SELECT 1 FROM send_queue WHERE lead_id=leads.id)""",
        (cid,))
    purged = 0
    for r in rows:
        site = (r["website"] or "").strip()
        keep, reason = discovery_verdict(
            r["business_name"] or "", site, r["category"] or niche, location,
            allow_no_url=not site)
        if keep:
            continue
        db.w("DELETE FROM leads WHERE id=?", (r["id"],))
        db.w("DELETE FROM lead_events WHERE lead_id=?", (r["id"],))
        purged += 1
        log.info("purge: deleted junk lead %r (%s): %s",
                 r["business_name"], site or "(no website)", reason)
    if purged:
        log.info("purge: campaign %s (%s) removed %d junk leads",
                 cid, camp.get("name"), purged)
    return purged


_STAGE_FNS = {
    "discover": _discover,
    "enrich": _enrich,
    "people": _people,
    "validate": _validate,
    "write": _write,
    "queue": _queue,
}


def tick_campaign(camp):
    """Advance one pipeline stage for one campaign. Returns (stage, note)."""
    stage = (camp.get("pipeline_stage") or "discover").strip() or "discover"
    if stage == "done":
        # Daily growth loop: at the start of a new day a finished pipeline
        # goes back to discover so fresh leads keep flowing in. Same day:
        # stays done (sending, follow-ups, reply scans continue via the
        # normal flow) -- unless legacy "unknown" rows still need the
        # (re-)validation the old blocked-probe behavior denied them.
        cur = _cursor(camp)
        if cur.get("disc_date") != _today_str():
            _set_stage(camp["id"], "discover")
            stage = "discover"
        elif _needs_revalidation(camp["id"]):
            _set_stage(camp["id"], "validate")
            stage = "validate"
        else:
            return "done", ""  # terminal for today
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
        try:
            purged = _purge_junk(camp)
        except Exception as e:
            log.warning("purge failed for campaign %s: %s", camp["id"], str(e)[:120])
            purged = 0
        if purged:
            summary["purged"] = summary.get("purged", 0) + purged
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
        "SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND email_verdict IN " + lq.queueable_verdict_sql(),
        (cid,))
    sent = one(
        "SELECT COUNT(DISTINCT lead_id) c FROM send_log WHERE campaign_id=? "
        "AND status IN ('sent','dry-run')", (cid,))
    replies = one("SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND replied=1", (cid,))
    queued = one("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=? AND status='pending'",
                 (cid,))
    camp = db.q("SELECT daily_discovery_target, pipeline_cursor FROM campaigns WHERE id=?",
                (cid,), one=True) or {}
    cur = {}
    try:
        cur = json.loads(camp.get("pipeline_cursor") or "{}")
    except Exception:
        pass
    discovered_today = (int(cur.get("disc_today") or 0)
                        if cur.get("disc_date") == _today_str() else 0)
    import decision_makers as dmm
    dm_leads, dm_total = dmm.dm_counts(cid)
    return {"found": found, "emails": emails, "validated": validated,
            "sent": sent, "replies": replies, "queued": queued,
            "discovered_today": discovered_today,
            "daily_target": _daily_target(camp),
            "dm_leads": dm_leads, "dm_total": dm_total}
