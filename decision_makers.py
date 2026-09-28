"""Decision-maker enrichment: find the real people behind each business.

Waleed's manual method, automated: ask Gemini (with web-search grounding,
the same thing Google AI Mode does) who the decision makers of a business
are, then CORROBORATE every suggestion before storing it. Accuracy is the
whole point: fewer verified contacts beat a long list of guesses.

Pipeline:
  1. deterministic extraction: fetch the business site's imprint/Impressum,
     about, team, and contact pages; pull person names + roles and every
     mailto: email. This is ground truth (self-published).
  2. Gemini with search grounding (native generateContent API with the
     googleSearch tool; the OpenAI-compatible endpoint used by ai_writer
     cannot do grounding): "who are the current decision makers of X?"
     -> name, current title, source URL for each.
  3. verification gate: an AI suggestion is stored ONLY when corroborated:
     the name appears on the business's own site/imprint, OR it appears in
     2+ independent web sources. Everything else is discarded, never
     stored as verified. Every stored contact keeps its source_url so a
     human can spot-check.
  4. emails: attached ONLY when a publicly listed email is tied to the
     person (a mailto link whose text names them, or an address whose
     local part contains their name). Never generated or guessed. A contact
     with no public email is stored with email='' and is never mailed.
     Found emails go through the standard email-verdict system; only
     valid/risky addresses are mailable.

No Gemini API key -> skipped gracefully, lead marked "pending"; the
pipeline never fails because of this module.
"""
import json
import logging
import re
import time
import urllib.request
import urllib.error
from urllib.parse import urljoin, urlparse

import db
import lead_quality as lq

log = logging.getLogger(__name__)

# Pages likely to name the people behind the business. German sites keep
# this in the legally required Impressum; English sites on about/team.
SITE_PATHS = ("/impressum", "/imprint", "/about", "/about-us", "/team",
              "/contact", "/kontakt", "/ueber-uns", "/company")
PAGE_PAUSE_S = 0.4
GEMINI_TIMEOUT_S = 45
MAX_PEOPLE_PER_LEAD_HARD = 4  # never store more than this per business

# Leadership role words (German + English). A name is only extracted when
# it appears next to one of these, so random names in page text are never
# picked up.
_ROLE_WORDS = (
    r"Gesch\u00e4ftsf\u00fchrer(?:in)?", r"Inhaber(?:in)?", r"CEO", r"Founder",
    r"Gr\u00fcnder(?:in)?", r"Owner", r"Managing Director",
    r"Gesch\u00e4ftsf\u00fchrung", r"Gesch\u00e4ftsleitung", r"Vorstand",
    r"Pr\u00e4sident(?:in)?", r"President", r"Direktor(?:in)?", r"Director",
    r"Partner(?:in)?", r"Leiter(?:in)?", r"Manager", r"Chef",
)
# A person name: 2-3 capitalized words (supports umlauts). Extra words
# must not be role words, so a following "Managing Director: ..." line is
# never absorbed into the name ("Anna Berg Managing").
_ROLE_LOOKAHEAD = r"(?!(?:%s)\b)" % "|".join(_ROLE_WORDS)
_NAME = (r"([A-Z\u00c4\u00d6\u00dc][\w\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc\u00df\-]+"
         r"(?:\s+%s[A-Z\u00c4\u00d6\u00dc][\w\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc\u00df\-]+){1,2})"
         % _ROLE_LOOKAHEAD)
_ROLE_RES = (
    # "Geschäftsführer: Max Mustermann" / "CEO - Max Mustermann"
    re.compile(r"(?:%s)\s*[:\-–]\s*%s" % ("|".join(_ROLE_WORDS), _NAME), re.I),
    # "Max Mustermann – Geschäftsführer"
    re.compile(r"%s\s+[–—-]\s*(?:%s)" % (_NAME, "|".join(_ROLE_WORDS)), re.I),
    # "Vertretungsberechtigter: Max Mustermann" (German imprint boilerplate)
    re.compile(r"Vertretungsberechtigt(?:er|e|es)?\s*[:\-–]?\s*%s" % _NAME, re.I),
)
_ROLE_FOR_TITLE_RES = re.compile(r"(%s)" % "|".join(_ROLE_WORDS), re.I)
_MAILTO_RE = re.compile(r'href=["\']mailto:([^"\'>?]+)', re.I)

DM_SYSTEM = """You identify the current decision makers of a small business from public web sources.
Reply with JSON only: {"people": [{"name": "...", "title": "...", "source_urls": ["...", "..."]}]}
Hard rules:
- Only real people with a current decision-making role: owner, founder, managing director, CEO, general manager, director. Never list employees without a leadership title.
- Every person MUST have 1-3 source_urls: the exact public pages where that specific person is named with their role (the business's own site/imprint, a news article, a directory page that names the person). A URL counts only if the person's name actually appears on that page.
- Never invent a person, a title, or a URL. Never reuse one person's sources for another person. If you cannot find any decision maker, return {"people": []}.
- Titles must be the person's current title exactly as the source states it."""


def _fetch_page(url):
    """Module-level indirection so tests can stub page fetches."""
    import enrich as _enrich
    return _enrich._fetch(url)


def _clean_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _visible_text(html):
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th",
               "dt", "dd", "a", "span", "div", "section", "article")


def _visible_blocks(html):
    """Visible text split by block-level element, so role/name patterns
    never bleed across paragraphs ("Owner: Anna Berg" + next paragraph's
    "Managing Director: ..." must not merge into one fake name)."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        blocks, seen = [], set()
        for el in soup.find_all(_BLOCK_TAGS):
            # Skip containers that hold smaller text blocks; take the
            # innermost ones so nothing is duplicated or merged.
            if el.find(("p", "h1", "h2", "h3", "h4", "h5", "h6", "li",
                        "td", "th", "dt", "dd")):
                continue
            t = el.get_text(" ", strip=True)
            if t and t not in seen:
                seen.add(t)
                blocks.append(t)
        return blocks
    except Exception:
        return [_visible_text(html)]


def _site_pages(website):
    """(base, [(path, url, html)]) for the homepage + people-likely pages."""
    base = _clean_url(website)
    if not base:
        return "", []
    pages = [("", base)]
    for p in SITE_PATHS:
        pages.append((p, urljoin(base + "/", p.lstrip("/"))))
    out = []
    seen = set()
    for path, url in pages:
        if url in seen:
            continue
        seen.add(url)
        if out:
            time.sleep(PAGE_PAUSE_S)
        html = _fetch_page(url)
        if html:
            out.append((path or "/", url, html))
    return base, out


def extract_site_people(website):
    """Deterministic ground truth from the business's own site.

    Returns (people, emails):
      people: [{name, title, source_url}] from role-anchored patterns
      emails: [{email, anchor}] for every mailto: link (anchor = link text)
    """
    _, pages = _site_pages(website)
    people, seen_names = [], set()
    emails, seen_emails = [], set()
    for path, url, html in pages:
        for text in _visible_blocks(html):
            for rx in _ROLE_RES:
                for m in rx.finditer(text):
                    groups = [g for g in m.groups() if g]
                    if not groups:
                        continue
                    name = groups[0].strip()
                    # The title is the role word in the full match
                    # ("Owner: Anna Berg" -> "Owner"); the name group alone
                    # never carries it.
                    tm = _ROLE_FOR_TITLE_RES.search(m.group(0))
                    title = tm.group(1).strip() if tm else ""
                    if not name or len(name.split()) < 2:
                        continue
                    key = lq.normalize_name(name)
                    if not key or key in seen_names:
                        continue
                    # Guard: skip strings that are clearly not names.
                    if len(name) > 60 or "@" in name or any(
                            w in key for w in ("gmbh", "ug", "impressum", "datenschutz")):
                        continue
                    seen_names.add(key)
                    people.append({"name": name, "title": title,
                                   "source_url": url})
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                m = _MAILTO_RE.search('href="%s"' % a["href"])
                if not m:
                    continue
                email = m.group(1).strip().lower()
                if not email or "@" not in email or email in seen_emails:
                    continue
                seen_emails.add(email)
                emails.append({"email": email,
                               "anchor": a.get_text(" ", strip=True)[:120]})
        except Exception:
            for m in _MAILTO_RE.finditer(html):
                email = m.group(1).strip().lower()
                if email and "@" in email and email not in seen_emails:
                    seen_emails.add(email)
                    emails.append({"email": email, "anchor": ""})
    return people, emails


def _name_tokens(name):
    return set(lq.normalize_name(name).split())


def names_match(a, b):
    """True when two names almost surely denote the same person: identical
    after normalization, or one is a token-subset of the other sharing at
    least 2 tokens ("Max Mustermann" vs "Mustermann, Max")."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    shared = ta & tb
    return len(shared) >= 2 and (ta <= tb or tb <= ta)


def _email_name_tokens(name):
    """Tokens usable for matching an email local part to a person."""
    return {t for t in _name_tokens(name) if len(t) >= 3}


def attach_email_to_person(name, site_emails):
    """Return the publicly listed email tied to this person, else "".

    Attached ONLY when the evidence is on the public page itself: the
    mailto link's text names the person, or the address's local part
    contains their name tokens (e.g. max.mustermann@, mmustermann@).
    Never generated, never guessed.
    """
    tokens = _email_name_tokens(name)
    if not tokens:
        return ""
    for e in site_emails or []:
        email = (e.get("email") or "").lower()
        if not email or "@" not in email:
            continue
        anchor_tokens = _name_tokens(e.get("anchor") or "")
        if tokens & anchor_tokens and len(tokens & anchor_tokens) >= 2:
            return email
        local = email.partition("@")[0].replace(".", "").replace("_", "").replace("-", "")
        if any(t in local for t in tokens):
            # avoid attaching role/generic inboxes: require a name token
            # AND the local part to not be purely generic
            if local not in ("info", "contact", "kontakt", "mail", "office",
                             "service", "hello", "hallo", "anfrage"):
                return email
    return ""


def _gemini_grounded_call(prompt):
    """Native Gemini generateContent with the googleSearch tool.

    Returns (people, chunk_domains): people=[{name,title,source_urls}],
    chunk_domains=[distinct grounded source domains]. Raises RuntimeError
    on any failure (no key, network, auth, unreadable response)."""
    import config
    key = (config.GEMINI_API_KEY or "").strip()
    if not key:
        raise RuntimeError("no Gemini API key configured")
    model = (config.GEMINI_MODEL or "gemini-3.8-flash").strip() or "gemini-3.8-flash"
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = json.dumps({
        "system_instruction": {"parts": [{"text": DM_SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.2, "maxOutputTokens": 1500},
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"Gemini search error ({e.code}): {detail or 'request failed'}".strip())
    except Exception as e:
        raise RuntimeError(f"Gemini search failed: {str(e)[:120]}")
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("Gemini returned no candidates.")
    parts = ((cands[0].get("content") or {}).get("parts") or [])
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    try:
        parsed = json.loads(text)
    except Exception:
        raise RuntimeError("Gemini returned an unreadable response.")
    chunks = ((cands[0].get("groundingMetadata") or {}).get("groundingChunks") or [])
    domains = []
    for ch in chunks:
        uri = ((ch or {}).get("web") or {}).get("uri") or ""
        d = urlparse(uri).netloc.lower()
        if d and d not in domains:
            domains.append(d)
    people = parsed.get("people") or []
    if not isinstance(people, list):
        raise RuntimeError("Gemini returned an unreadable response.")
    return people, domains


# Module-level indirection so tests can stub the AI call.
_gemini_call = _gemini_grounded_call


def _dm_prompt(lead, campaign):
    name = (lead.get("business_name") or "").strip() or "(unknown)"
    site = (lead.get("website") or "").strip() or "(none)"
    loc = ((campaign or {}).get("location") or "").strip()
    niche = ((campaign or {}).get("niche") or lead.get("category") or "").strip()
    return (f"Business: {name}\nWebsite: {site}\n"
            f"Location: {loc or '(unknown)'}\nCategory: {niche or '(unknown)'}\n"
            "Who are the current decision makers (owner, founder, managing "
            "director, CEO, general manager, director) of this business? "
            "Search the web and report only real, currently listed people.")


def _name_in_text(name, text):
    """True when the person's full name appears in a page's visible text
    (normalized comparison, so 'Mustermann, Max' style variants count)."""
    norm = lq.normalize_name(name)
    if not norm:
        return False
    tnorm = lq.normalize_name(text or "")
    if norm in tnorm:
        return True
    parts = norm.split()
    if len(parts) >= 2 and " ".join(reversed(parts)) in tnorm:
        return True
    return False


def _corroborated(name, source_urls, site_people, own_domain):
    """The verification gate. Returns (ok, evidence_url).

    ok is True only when the person is corroborated by evidence tied to
    THAT person specifically:
      - the name matches a person extracted from the business's own
        site/imprint (self-published ground truth), or
      - the name is found in the visible text of pages from 2+ independent
        domains (the business's own domain never counts), fetched live and
        checked for the actual name. A domain the AI merely named, without
        the person on the fetched page, never counts.
    """
    for p in site_people or []:
        if names_match(name, p.get("name") or ""):
            return True, p.get("source_url") or ""
    confirmed, first_url = [], ""
    for url in (source_urls or [])[:3]:
        url = (url or "").strip()
        if not url:
            continue
        dom = urlparse(url).netloc.lower()
        if not dom or dom == (own_domain or "") or dom in confirmed:
            continue
        try:
            text = _visible_text(_fetch_page(url) or "")
        except Exception:
            continue
        if text and _name_in_text(name, text):
            confirmed.append(dom)
            if not first_url:
                first_url = url
            if len(confirmed) >= 2:
                break
        time.sleep(PAGE_PAUSE_S)
    return (len(confirmed) >= 2), first_url


def _contact_exists(lead_id, name):
    rows = db.q("SELECT id, name FROM contacts WHERE lead_id=?", (lead_id,))
    return any(names_match(name, r["name"]) for r in rows)


def _set_dm_status(lead_id, status, note=""):
    db.w("UPDATE leads SET dm_status=? WHERE id=?", (status, lead_id))
    if note:
        log.info("decision-makers lead %s -> %s: %s", lead_id, status, note)


def _store_contact(lead_id, name, title, email, source_url):
    return db.w(
        "INSERT INTO contacts (lead_id, name, title, email, source_url, verified, created_at)"
        " VALUES (?,?,?,?,?,1,?)",
        (lead_id, name[:120], (title or "")[:80], (email or "")[:160],
         (source_url or "")[:500], time.time()))


def enrich_lead(lead, campaign=None, max_contacts=3):
    """Find and store verified decision makers for one lead.

    Returns (status, added): status is 'done', 'pending', or 'skipped';
    added is the number of contacts stored. Never raises: any failure is
    caught by the caller and the lead is marked pending for retry.
    """
    import config
    lead_id = lead["id"]
    website = (lead.get("website") or "").strip()
    if not website:
        _set_dm_status(lead_id, "skipped", "no website to search")
        return "skipped", 0
    if not (config.GEMINI_API_KEY or "").strip():
        _set_dm_status(lead_id, "pending", "waiting for a Gemini API key")
        return "pending", 0
    max_contacts = max(1, min(MAX_PEOPLE_PER_LEAD_HARD,
                             int(max_contacts or 3)))
    try:
        site_people, site_emails = extract_site_people(website)
    except Exception as e:
        log.warning("decision-makers: site extraction failed for lead %s: %s",
                    lead_id, str(e)[:100])
        site_people, site_emails = [], []
    try:
        suggestions, _grounding_domains = _gemini_call(_dm_prompt(lead, campaign or {}))
    except Exception as e:
        _set_dm_status(lead_id, "pending",
                       f"search failed, will retry: {str(e)[:100]}")
        return "pending", 0
    own_domain = lq.normalize_domain(website)
    kept = []

    def consider(name, title, source_url):
        name = (name or "").strip()
        if not name or len(name.split()) < 2 or len(name) > 80:
            return
        if _contact_exists(lead_id, name):
            return
        if any(names_match(name, k["name"]) for k in kept):
            return
        email = attach_email_to_person(name, site_emails)
        kept.append({"name": name, "title": (title or "").strip(),
                     "email": email, "source_url": (source_url or "").strip()})

    # Strongest evidence first: people published on the business's own
    # site/imprint (ground truth, no AI needed).
    for p in site_people:
        consider(p["name"], p["title"], p["source_url"])
        if len(kept) >= max_contacts:
            break
    # Then AI suggestions that pass the per-person verification gate.
    if len(kept) < max_contacts:
        for s in suggestions or []:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            urls = s.get("source_urls") or []
            if not urls and s.get("source_url"):
                urls = [s.get("source_url")]
            ok, evidence = _corroborated(name, urls, site_people, own_domain)
            if not ok:
                log.info("decision-makers: discarded uncorroborated suggestion %r for lead %s",
                         name[:60], lead_id)
                continue
            consider(name, s.get("title"), evidence)
            if len(kept) >= max_contacts:
                break
    added = 0
    for k in kept:
        _store_contact(lead_id, k["name"], k["title"], k["email"], k["source_url"])
        added += 1
    _set_dm_status(lead_id, "done",
                   f"{added} verified decision maker(s) stored" if added
                   else "searched; no verifiable decision makers found")
    return "done", added


def get_contacts(lead_id):
    """All contacts for a lead, verified first."""
    return db.q("SELECT * FROM contacts WHERE lead_id=? ORDER BY verified DESC, id",
                (lead_id,))


def mailable_contacts(lead_id, limit=3):
    """Verified contacts with a validated public email. Only these are
    ever mailed. limit caps contacts per business (default 3)."""
    return db.q(
        "SELECT * FROM contacts WHERE lead_id=? AND verified=1 AND email<>'' "
        "AND email_verdict IN " + lq.queueable_verdict_sql() + " ORDER BY id LIMIT ?",
        (lead_id, max(1, int(limit or 3))))


def dm_counts(campaign_id):
    """(leads_with_contacts, total_contacts) for a campaign."""
    row = db.q("SELECT COUNT(DISTINCT lead_id) c FROM contacts WHERE verified=1 "
               "AND lead_id IN (SELECT id FROM leads WHERE campaign_id=?)",
               (campaign_id,), one=True)
    row2 = db.q("SELECT COUNT(*) c FROM contacts WHERE verified=1 "
                "AND lead_id IN (SELECT id FROM leads WHERE campaign_id=?)",
                (campaign_id,), one=True)
    return ((row["c"] if row else 0) or 0, (row2["c"] if row2 else 0) or 0)


def lead_dm_counts(campaign_id):
    """{lead_id: verified contact count} for the campaign's leads table."""
    rows = db.q("SELECT lead_id, COUNT(*) c FROM contacts WHERE verified=1 "
                "AND lead_id IN (SELECT id FROM leads WHERE campaign_id=?) "
                "GROUP BY lead_id", (campaign_id,))
    return {r["lead_id"]: r["c"] for r in rows}
