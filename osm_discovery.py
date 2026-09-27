"""OpenStreetMap business discovery: real, verified businesses, globally.

Two steps, both free, keyless, and legit (ODbL-licensed data):
  1. Nominatim geocoding: free-text location -> lat/lon. Usage policy
     respected: max 1 request/sec, identifying User-Agent, one geocode
     per campaign location (cached in the pipeline cursor).
  2. Overpass API: real POIs near the coordinates from a niche -> OSM
     tag map. POST requests (GET is flaky), nwr (nodes+ways+relations)
     with `out center tags`, one tag-group per call so a serverless tick
     stays fast. Any failure falls through to web search, never a hard
     error.

Research basis: docs/discovery-research.md. OSM is strong for
consumer foot-traffic POIs in well-mapped cities (fitness, restaurants,
salons, cafes: ~70% carry a website tag) and thin for trades
(plumbers/electricians: essentially unmapped) and non-Western cities.
The pipeline therefore treats OSM as one source alongside web search,
not a replacement.
"""
import json
import logging
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

UA = {"User-Agent": "TechXpert-Outreach/1.0 (cold-email lead discovery; contact via app settings)"}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

OSM_RADIUS_M = 10000      # search radius around the geocoded point
OSM_TIMEOUT_S = 45        # per Overpass call
NOMINATIM_MIN_GAP_S = 1.1  # usage policy: max 1 request/second

_last_nominatim_call = 0.0


# Niche keyword -> list of OSM (key, value) tag groups. Each group is one
# Overpass query, run on its own tick. Matched case-insensitively against
# the campaign niche; first matching niche wins. "None" entries document
# niches that were researched and found unmapped (web search covers them).
NICHE_OSM_TAGS = {
    "gym": [("leisure", "fitness_centre")],
    "fitness": [("leisure", "fitness_centre")],
    "yoga": [("leisure", "fitness_centre")],
    "restaurant": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "coffee": [("amenity", "cafe")],
    "bar": [("amenity", "bar")],
    "fast food": [("amenity", "fast_food")],
    "salon": [("shop", "hairdresser"), ("shop", "beauty")],
    "hair": [("shop", "hairdresser")],
    "beauty": [("shop", "beauty")],
    "spa": [("shop", "beauty"), ("leisure", "spa")],
    "dentist": [("amenity", "dentist")],
    "dental": [("amenity", "dentist")],
    "clinic": [("amenity", "clinic")],
    "pharmacy": [("amenity", "pharmacy")],
    "veterinary": [("amenity", "veterinary")],
    "vet ": [("amenity", "veterinary")],
    "hotel": [("tourism", "hotel")],
    "car repair": [("shop", "car_repair")],
    "auto repair": [("shop", "car_repair")],
    "lawyer": [("office", "lawyer")],
    "law firm": [("office", "lawyer")],
    # Researched, essentially unmapped in OSM -> web search covers these.
    "plumber": None,
    "electrician": None,
    "hvac": None,
    "roofing": None,
}

# Longest-key-first so "fast food" beats "food", "car repair" beats "repair".
_NICHE_KEYS = sorted(NICHE_OSM_TAGS, key=len, reverse=True)


def niche_tag_groups(niche):
    """OSM (key, value) tag groups for a niche, or None when unmapped.

    Each returned group is one Overpass query (one tick). None means "OSM
    has no useful coverage for this niche" (researched: trades like
    plumber/electrician/hvac).
    """
    n = (niche or "").lower()
    for key in _NICHE_KEYS:
        if key in n:
            return NICHE_OSM_TAGS[key]
    return None


def geocode(location):
    """Free-text location -> (lat, lon) via Nominatim, or (None, None).

    Respects the usage policy: identifying User-Agent, >=1s between
    calls. Callers cache the result (one geocode per campaign location).
    """
    global _last_nominatim_call
    loc = (location or "").strip()
    if not loc:
        return None, None
    wait = NOMINATIM_MIN_GAP_S - (time.time() - _last_nominatim_call)
    if wait > 0:
        time.sleep(wait)
    url = NOMINATIM_URL + "?" + urllib.parse.urlencode(
        {"q": loc, "format": "json", "limit": 1})
    try:
        req = urllib.request.Request(url, headers=UA)
        _last_nominatim_call = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        log.warning("nominatim geocode failed for %r: %s", loc, e)
    return None, None


def _overpass_post(query):
    """POST one Overpass query; returns parsed JSON or None on failure."""
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=OSM_TIMEOUT_S) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("overpass query failed: %s", str(e)[:120])
        return None


def query_tag_group(lat, lon, key, value, radius_m=OSM_RADIUS_M):
    """One (key, value) tag group near a point -> raw OSM elements."""
    q = (f'[out:json][timeout:40];'
         f'(node["{key}"="{value}"](around:{radius_m},{lat},{lon});'
         f'way["{key}"="{value}"](around:{radius_m},{lat},{lon});'
         f'relation["{key}"="{value}"](around:{radius_m},{lat},{lon}););'
         f'out center tags;')
    res = _overpass_post(q)
    if res is None:
        # One retry; transient 504s were observed in research.
        time.sleep(3)
        res = _overpass_post(q)
    return (res or {}).get("elements", [])


def _compose_address(tags):
    parts = [tags.get("addr:housenumber", ""), tags.get("addr:street", "")]
    street = " ".join(p for p in parts if p).strip()
    city = tags.get("addr:city", "") or tags.get("addr:suburb", "")
    postcode = tags.get("addr:postcode", "")
    return ", ".join(p for p in (street, postcode, city) if p)


def parse_element(el):
    """One Overpass element -> lead dict, or None when unusable.

    Keeps only real, contactable businesses: must have a name, and must
    have a website or phone tag (a lead with neither can never be
    email-enriched or called).
    """
    tags = (el or {}).get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None
    website = (tags.get("website") or tags.get("contact:website") or "").strip()
    phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
    if not website and not phone:
        return None
    if website and not website.startswith("http"):
        website = "https://" + website
    return {
        "business_name": name,
        "address": _compose_address(tags),
        "phone": phone,
        "website": website,
        "source": "osm",
    }


def discover(niche, location, lat=None, lon=None, group_index=0):
    """One OSM discovery step: the group_index-th tag group for the niche.

    Returns (leads, next_group_index, exhausted). exhausted is True when
    there are no more tag groups (or the niche is unmapped / geocoding
    failed). Never raises: failures return ([], group_index, False) so
    the caller can fall through to web search and retry later.
    """
    groups = niche_tag_groups(niche)
    if not groups:
        return [], 0, True  # unmapped niche (e.g. trades): web search covers it
    if group_index >= len(groups):
        return [], group_index, True
    if lat is None or lon is None:
        lat, lon = geocode(location)
        if lat is None:
            log.warning("osm discover: geocoding failed for %r", location)
            return [], group_index, False
    key, value = groups[group_index]
    elements = query_tag_group(lat, lon, key, value)
    leads = []
    for el in elements:
        lead = parse_element(el)
        if lead:
            lead["notes"] = f"osm: {key}={value} near {location}"
            leads.append(lead)
    log.info("osm discover: %s %s=%s near %s -> %d usable leads",
             niche, key, value, location, len(leads))
    nxt = group_index + 1
    return leads, nxt, nxt >= len(groups)
