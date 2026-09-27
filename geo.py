"""Location intelligence for global discovery: free-text location ->
city + country + language, and multilingual niche translation.

Pure functions, no network (geocoding lives in osm_discovery.py).
Used by pipeline.build_discovery_queries so ANY niche + ANY location on
the globe gets local-language search queries, not just Berlin.

Research basis: docs/discovery-research.md (live Nominatim/Overpass tests).
"""
import re

# Country -> primary language (ISO 639-1). Keys are lowercase country names,
# common aliases, and 2-letter codes. Matching is longest-key-first with
# word boundaries, so "Rio de Janeiro, Brazil" -> pt (not de from "de"),
# and "Austin TX" finds nothing -> English fallback.
#
# Only non-English mappings are listed; anything unmatched falls back to
# "en". 40+ countries covered.
COUNTRY_LANGUAGES = {
    # German
    "deutschland": "de", "germany": "de", "austria": "de",
    "osterreich": "de", "österreich": "de", "schweiz": "de",
    # French
    "france": "fr", "belgique": "fr", "belgium": "fr", "luxembourg": "fr",
    "suisse": "fr", "monaco": "fr", "frankreich": "fr",
    # Spanish
    "espana": "es", "españa": "es", "spain": "es", "mexico": "es",
    "argentina": "es", "colombia": "es", "chile": "es", "peru": "es",
    "venezuela": "es", "ecuador": "es", "guatemala": "es", "cuba": "es",
    "bolivia": "es", "dominican republic": "es", "honduras": "es",
    "paraguay": "es", "el salvador": "es", "nicaragua": "es",
    "costa rica": "es", "panama": "es", "uruguay": "es",
    "puerto rico": "es", "spanien": "es",
    # Portuguese
    "portugal": "pt", "brazil": "pt", "brasil": "pt",
    # Italian
    "italy": "it", "italia": "it", "italien": "it",
    # Dutch
    "netherlands": "nl", "nederland": "nl", "holland": "nl",
    "niederlande": "nl",
    # 2-letter codes (word-boundary matched; longest-first wins over these)
    "de": "de", "at": "de", "ch": "de",
    "fr": "fr", "be": "fr", "lu": "fr",
    "es": "es", "mx": "es", "ar": "es", "co": "es", "cl": "es", "pe": "es",
    "pt": "pt", "br": "pt",
    "it": "it",
    "nl": "nl",
}

# Pre-sorted longest-first for deterministic matching.
_COUNTRY_KEYS = sorted(COUNTRY_LANGUAGES, key=len, reverse=True)


def parse_location(location):
    """Split free text into city + country.

    "Berlin, Germany" -> ("Berlin", "Germany")
    "Lyon, France"    -> ("Lyon", "France")
    "Austin TX"       -> ("Austin", "")      (US state abbrev stripped)
    "Paris"           -> ("Paris", "")
    """
    loc = (location or "").strip()
    if not loc:
        return "", ""
    parts = [p.strip() for p in loc.split(",")]
    city = parts[0]
    # Strip a trailing US/CA-style state/province abbreviation: "Austin TX".
    city = re.sub(r"\s+[A-Z]{2}$", "", city).strip()
    country = parts[-1] if len(parts) > 1 else ""
    return city, country


def location_language(location):
    """ISO 639-1 language code for a free-text location; 'en' fallback.

    Matches country names/aliases/codes longest-first with word
    boundaries, so "Rio de Janeiro, Brazil" -> "pt" and plain "Paris"
    (no country given) -> "en".
    """
    loc = (location or "").lower()
    if not loc.strip():
        return "en"
    for key in _COUNTRY_KEYS:
        if re.search(r"\b" + re.escape(key) + r"\b", loc):
            return COUNTRY_LANGUAGES[key]
    return "en"


# Multilingual niche dictionary: language -> {english phrase -> local phrase}.
# Generic mechanism (see translate_niche): word-boundary, longest-first,
# case-insensitive. New languages/words are data-only additions.
NICHE_TRANSLATIONS = {
    "de": {
        "fitness centers": "Fitnesscenter", "fitness center": "Fitnesscenter",
        "fitness centres": "Fitnesscenter", "fitness centre": "Fitnesscenter",
        "gyms": "Fitnessstudios", "gym": "Fitnessstudio",
        "restaurants": "Restaurants", "restaurant": "Restaurant",
        "plumbers": "Klempner", "plumber": "Klempner",
        "electricians": "Elektriker", "electrician": "Elektriker",
        "dentists": "Zahnärzte", "dentist": "Zahnarzt",
        "salons": "Salons", "salon": "Salon",
        "hvac": "Klimaanlagen",
        "and": "und",
    },
    "fr": {
        "fitness centers": "salles de sport", "fitness center": "salle de sport",
        "fitness centres": "salles de sport", "fitness centre": "salle de sport",
        "gyms": "salles de sport", "gym": "salle de sport",
        "restaurants": "restaurants", "restaurant": "restaurant",
        "plumbers": "plombiers", "plumber": "plombier",
        "electricians": "électriciens", "electrician": "électricien",
        "dentists": "dentistes", "dentist": "dentiste",
        "salons": "salons", "salon": "salon",
        "hvac": "climatisation",
        "and": "et",
    },
    "es": {
        "fitness centers": "gimnasios", "fitness center": "gimnasio",
        "fitness centres": "gimnasios", "fitness centre": "gimnasio",
        "gyms": "gimnasios", "gym": "gimnasio",
        "restaurants": "restaurantes", "restaurant": "restaurante",
        "plumbers": "fontaneros", "plumber": "fontanero",
        "electricians": "electricistas", "electrician": "electricista",
        "dentists": "dentistas", "dentist": "dentista",
        "salons": "salones", "salon": "salón",
        "hvac": "climatización",
        "and": "y",
    },
    "pt": {
        "fitness centers": "academias", "fitness center": "academia",
        "fitness centres": "academias", "fitness centre": "academia",
        "gyms": "academias", "gym": "academia",
        "restaurants": "restaurantes", "restaurant": "restaurante",
        "plumbers": "encanadores", "plumber": "encanador",
        "electricians": "eletricistas", "electrician": "eletricista",
        "dentists": "dentistas", "dentist": "dentista",
        "salons": "salões", "salon": "salão",
        "hvac": "climatização",
        "and": "e",
    },
    "it": {
        "fitness centers": "palestre", "fitness center": "palestra",
        "fitness centres": "palestre", "fitness centre": "palestra",
        "gyms": "palestre", "gym": "palestra",
        "restaurants": "ristoranti", "restaurant": "ristorante",
        "plumbers": "idraulici", "plumber": "idraulico",
        "electricians": "elettricisti", "electrician": "elettricista",
        "dentists": "dentisti", "dentist": "dentista",
        "salons": "saloni", "salon": "salone",
        "hvac": "climatizzazione",
        "and": "e",
    },
    "nl": {
        "fitness centers": "sportscholen", "fitness center": "sportschool",
        "fitness centres": "sportscholen", "fitness centre": "sportschool",
        "gyms": "sportscholen", "gym": "sportschool",
        "restaurants": "restaurants", "restaurant": "restaurant",
        "plumbers": "loodgieters", "plumber": "loodgieter",
        "electricians": "elektriciens", "electrician": "elektricien",
        "dentists": "tandartsen", "dentist": "tandarts",
        "salons": "salons", "salon": "salon",
        "hvac": "airco",
        "and": "en",
    },
}


def translate_niche(niche, lang):
    """Translate common niche words into the local language.

    Generic: word-boundary, longest-phrase-first, case-insensitive.
    Falls back to the English niche when nothing matches (or when lang
    is English/unknown).
    """
    mapping = NICHE_TRANSLATIONS.get(lang or "en") or {}
    out = niche or ""
    for src in sorted(mapping, key=len, reverse=True):
        out = re.sub(r"\b" + re.escape(src) + r"\b", mapping[src], out,
                     flags=re.IGNORECASE)
    return out
