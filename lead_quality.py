"""Lead data quality: the single home for every data-quality decision.

Waleed's rule: data quality decides the campaign. Broken or guessed data
fails the campaign; authentic, verified data wins it. So the pipeline, the
manual jobs, CSV import, and the UI all apply the same rules from here:

  validated email : only emails with a queueable verdict may enter the send
                    queue (see QUEUEABLE_VERDICTS below)
  dedup           : normalize business names and domains; merge duplicates
                    instead of creating new lead rows
  fit             : flag likely chains/franchises; the ICP is independent
                    gyms (1 to 3 locations, no in-house marketing team)

Nothing here ever guesses an email address. If no public email is found and
validated, the lead is skipped. That rule is sacred and must not be weakened.
"""
import re
import unicodedata

import db

# ---------------------------------------------------------------------------
# What "validated" means.
#
# A lead's email is VALIDATED (queueable) only when its email_verdict is
# "valid" or "risky". Every other verdict is held back, no exceptions:
#
#   valid   : syntax OK, domain is not disposable, the domain has MX records
#             (it can receive mail), AND the SMTP probe confirmed the mailbox
#             accepts mail on a non-catch-all server.
#   risky   : syntax OK, not disposable, MX records exist, but the server is
#             catch-all (accepts any address), so the mailbox itself could not
#             be confirmed. Acceptable for cold outreach, flagged as risky.
#
#   invalid : bad syntax, disposable domain, no MX records, or the mailbox
#             was rejected by the server. The address is cleared so it can
#             never enter the send queue.
#   unknown : the domain has MX records but the SMTP probe was blocked,
#             deferred, or greylisted, so the mailbox could not be confirmed.
#             Held back on purpose: we never guess.
#   none/"" : no public email was ever found (or it was never validated).
#             Nothing to send to.
# ---------------------------------------------------------------------------
QUEUEABLE_VERDICTS = ("valid", "risky")


def is_queueable_verdict(verdict):
    """True only for validated emails (see the definition above)."""
    return (verdict or "").strip().lower() in QUEUEABLE_VERDICTS


def queueable_verdict_sql():
    """The SQL fragment for the verdict gate, e.g. for IN clauses."""
    return "(" + ",".join(f"'{v}'" for v in QUEUEABLE_VERDICTS) + ")"


# ---------------------------------------------------------------------------
# Dedup: normalize business names and domains so near-duplicates merge.
# ---------------------------------------------------------------------------

_UMLAUT_FOLD = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}

# Legal-entity suffixes in normalized form (punctuation already stripped).
# Stripped only as a trailing token, repeatedly, so "McFIT Berlin GmbH"
# and "McFIT Berlin" normalize identically.
_LEGAL_SUFFIXES = (
    "gmbh co kg", "gmbh", "gbr", "ug", "ag", "ev",
    "ltd", "llc", "inc", "corp", "limited", "co kg",
)


def normalize_name(name):
    """Lowercase, fold umlauts, drop punctuation, collapse whitespace, and
    strip trailing legal-entity suffixes (GmbH, UG, Ltd, ...)."""
    n = (name or "").strip().lower()
    for a, b in _UMLAUT_FOLD.items():
        n = n.replace(a, b)
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    changed = True
    while changed:
        changed = False
        for suf in _LEGAL_SUFFIXES:
            if n != suf and n.endswith(" " + suf):
                n = n[:-(len(suf) + 1)].strip()
                changed = True
                break
    return n


def normalize_domain(website_or_domain):
    """Lowercase host without scheme, path, port, or leading www."""
    d = (website_or_domain or "").strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = re.split(r"[/?#]", d, maxsplit=1)[0]
    d = d.split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    return d.rstrip(".").strip()


def find_duplicate_lead(campaign_id, name, website):
    """Return an existing lead row in this campaign matching the normalized
    domain or the normalized business name, else None. Domain match wins."""
    rows = db.q("SELECT * FROM leads WHERE campaign_id=?", (campaign_id,))
    dom = normalize_domain(website)
    if dom:
        for r in rows:
            if normalize_domain(r["website"]) == dom:
                return r
    nm = normalize_name(name)
    if nm:
        for r in rows:
            if normalize_name(r["business_name"]) == nm:
                return r
    return None


def merge_lead_fields(lead_id, name="", address="", phone="", website=""):
    """Fill gaps on an existing lead from newly discovered data. Never
    overwrites a field the lead already has; prefers the longer business
    name when the new one is more specific. Returns True when merged."""
    lead = db.q("SELECT * FROM leads WHERE id=?", (lead_id,), one=True)
    if not lead:
        return False
    updates = {}
    if not (lead["address"] or "").strip() and (address or "").strip():
        updates["address"] = address.strip()
    if not (lead["phone"] or "").strip() and (phone or "").strip():
        updates["phone"] = phone.strip()
    if not (lead["website"] or "").strip() and (website or "").strip():
        updates["website"] = website.strip()
    if (name or "").strip() and len(name.strip()) > len((lead["business_name"] or "")):
        updates["business_name"] = name.strip()
    if not updates:
        return False
    sets = ", ".join(f"{k}=?" for k in updates)
    db.w(f"UPDATE leads SET {sets} WHERE id=?", tuple(updates.values()) + (lead_id,))
    return True


# ---------------------------------------------------------------------------
# Chain / franchise detection. The ICP is independent businesses
# (1 to 3 locations, no in-house marketing team), so likely chains get
# flagged as "possible_chain" for the user to review.
#
# fit values stored on leads.fit:
#   "possible_chain" : a chain signal was found (see below)
#   "independent"   : name and homepage were checked, no chain signal found.
#                     This means "no chain signals found", not a verified audit.
#   ""              : not checked yet
# ---------------------------------------------------------------------------

# Known gym/fitness chains (normalized substrings). Extend per niche as
# campaigns expand; matching is by substring on the normalized name.
CHAIN_NAMES = (
    "mcfit", "fitx", "john reed", "clever fit", "cleverfit", "kieser",
    "mrs sporty", "mrssporty", "injoy", "fitness first", "holmes place",
    "easyfitness", "fitstar", "fit star", "jumpers", "venicebeach",
    "xtrafit", "world gym", "golds gym", "anytime fitness", "crunch fitness",
    "planet fitness", "la fitness", "equinox", "virgin active", "puregym",
    "the gym group", "body street", "bodystreet", "primofit",
)

# Wording on a business site that signals a franchise or many locations.
_FRANCHISE_RES = tuple(re.compile(p, re.I) for p in (
    r"franchise",
    r"partner werden",
    r"franchise[-\s]?partner",
    r"lizenzpartner",
))
# Location-listing words; 4+ mentions suggest 4+ locations.
_LOCATION_RES = tuple(re.compile(p, re.I) for p in (
    r"standorte", r"filialen", r"\bclubs\b", r"locations", r"studios\b",
))
_LOCATION_MENTION_THRESHOLD = 4


def detect_fit(name, niche="", page_text=""):
    """Return "possible_chain" when chain signals are found, else "".

    Signals: the normalized name contains a known chain brand; the site
    text uses franchise wording; or the site text mentions locations
    4+ times. Name-only checks run at discovery time; page_text is added
    when the homepage has been fetched (enrichment).
    """
    nm = normalize_name(name)
    if nm:
        for brand in CHAIN_NAMES:
            if brand in nm:
                return "possible_chain"
    text = (page_text or "").lower()
    if text:
        for rx in _FRANCHISE_RES:
            if rx.search(text):
                return "possible_chain"
        mentions = sum(len(rx.findall(text)) for rx in _LOCATION_RES)
        if mentions >= _LOCATION_MENTION_THRESHOLD:
            return "possible_chain"
    return ""
