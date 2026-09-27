# Discovery research: free, legit, global business-data sources

Researched 2026-09-28 with live API calls (no assumptions). Raw captures in
`/tmp/osm_research/` (ephemeral; key numbers copied here).

## Method

- **Nominatim** (geocoding): live `search` calls for 4 varied locations,
  proper `User-Agent`, 1.2s spacing between requests (usage policy: max
  1 req/sec, identifying UA).
- **Overpass API**: live queries against `overpass-api.de`. Finding: plain
  GET requests are flaky from our network (406/504/truncated reads);
  **POST works reliably** and is what Overpass docs recommend anyway.
  Two public mirrors (`overpass.kumi.systems`, `overpass.nchc.org.tw`)
  timed out / disconnected from here, so the implementation uses the main
  instance with POST, timeout, one retry, and graceful fallback.
- **Alternatives**: web search on Yelp Fusion / Foursquare / Google Places
  free tiers (2026 state).

## 1. Nominatim geocoding (all 4 locations resolve)

| Location string      | lat / lon              | display name (truncated) | country_code |
|----------------------|------------------------|--------------------------|--------------|
| Berlin, Germany      | 52.5174, 13.3951       | Berlin, Deutschland      | de           |
| Lyon, France         | 45.7578, 4.8320        | Lyon, ... France         | fr           |
| Austin TX            | 30.2711, -97.7437      | Austin, Travis County, Texas, United States | us |
| Lahore, Pakistan     | 31.5657, 74.3142       | لاہور، ... پاکستان (Urdu script) | pk     |

Notes:
- Works globally, including non-Western cities and non-Latin scripts.
- `country_code` is a reliable ISO signal for language mapping (better
  than parsing country names out of free text).
- Usage policy respected in implementation: 1 req/sec max, identifying
  User-Agent, one geocode per campaign location cached in the campaign
  cursor (no repeated geocoding).

## 2. Overpass data quality (Berlin Mitte)

Queries: `nwr` (nodes + ways, `out center tags`), POST, small bboxes.

| Niche (OSM tags)                    | Results (0.8 km²) | % with name | % with website | % with phone | % with addr |
|-------------------------------------|-------------------|-------------|----------------|--------------|-------------|
| gym/fitness (`leisure=fitness_centre`) | 4              | 100%        | 75%            | 50%          | 100%        |
| restaurant (`amenity=restaurant`)    | 49                | 100%        | 73%            | n/m          | n/m         |
| hair salon (`shop=hairdresser`)      | 6                 | 100%        | 67%           | 33%          | 50%         |
| cafe (`amenity=cafe`)                | exists            | high        | high           | n/m          | n/m         |
| plumber (`craft=plumber`)           | **0**             | -           | -              | -            | -           |
| dentist (`amenity=dentist`)         | 0 in test bbox    | sparse      | sparse         | -            | -           |
| car repair (`shop=car_repair`)      | 0 in test bbox    | sparse      | sparse         | -            | -           |
| lawyer (`office=lawyer`)            | 0 in test bbox    | sparse      | sparse         | -            | -           |

Sample real businesses returned: "Bikram Yoga Berlin-Mitte",
"Ladycompany - Fitness für Frauen", "Yogatribe", "Beat81", "Eden",
"Ciccia", "Mühle", "Mod's Hair Paris". All with street addresses;
websites present on ~2/3 to 3/4 of POIs.

**Judgment: data is real and good where it exists.** Names are genuine
business names (not listicles/aggregators), addresses are structured
(`addr:street/housenumber/postcode/city`), websites common enough that
the email-enrichment stage has something to work with.

## 3. Coverage limits (design drivers)

1. **Ways matter.** Restaurants appear as both nodes and ways; a
   nodes-only query undercounts. Implementation uses `nwr` + `out center`.
2. **Trades are essentially unmapped.** `craft=plumber` returned zero in
   Berlin; electricians/HVAC will be the same. OSM cannot be the primary
   source for trade niches; web search must stay primary there.
3. **Non-Western POI coverage is thin.** Zero `leisure=fitness_centre`
   within 8 km of Lahore. OSM is Western-city-biased; web search is the
   fallback that keeps "any location on the globe" working.
4. **Response size / reliability.** One 5 km fitness query 504'd; large
   responses get truncated from some networks. Implementation: moderate
   radius (10 km), one tag-group per tick, timeout + one retry, mirrors
   not relied upon, any failure falls through to web search (never a
   hard error, never a stuck pipeline).
5. **Contactability.** OSM leads without a website can't be email-enriched
   and would inflate lead counts without ever becoming sendable.
   Implementation keeps OSM leads only when a website or phone tag is
   present.

## 4. Alternatives checked

- **Yelp Fusion**: sunsetted free unlimited commercial use in 2019;
  now paid program with a limited free tier (~500 req/day per third-party
  reports), requires API key + app approval, US-centric, weak on trades/B2B
  and outside the US. Rejected: key friction + coverage gaps.
- **Foursquare Places**: 100k free req/month, but only ~40% of venues have
  phones and ~30% websites (third-party benchmark). Too thin on contact
  data for email outreach. Rejected.
- **Google Places**: paid, requires billing. Rejected (user friction).
- **Apify/scrapers**: paid actors, ToS gray area. Rejected ("legit way").

**Decision: OpenStreetMap (Nominatim + Overpass) as the keyless, global,
ODbL-licensed source, paired with the existing web-search discovery.**
OSM first for well-mapped niches, web search always as complement/fallback.
Attribution "© OpenStreetMap contributors" added to the app footer
(ODbL requirement).

## 5. What the implementation does (traceable to evidence)

- `geo.py`: offline location parsing + country→language map (40+ countries,
  longest-match-first so "Rio de Janeiro, Brazil" → pt, not de) +
  multilingual niche dictionary (de/fr/es/pt/it/nl, word-boundary,
  longest-first, case-insensitive; new languages are data-only).
- `osm_discovery.py`: Nominatim geocode (cached, 1 req/sec, UA) →
  Overpass `nwr` POST queries from a niche→tags map (12 niches; trades
  included but documented as sparse) → lead dicts (name/address/phone/
  website). Skips unnamed or contact-less entries.
- Pipeline: each discover tick tries one OSM tag-group (radius 10 km);
  on new leads the tick ends, otherwise it falls through to the existing
  web-search rotation. OSM results pass through the same junk filter
  (`discovery_verdict(..., allow_no_url=True)`), the same dedup
  (`find_duplicate_lead` merges OSM address/phone into existing rows),
  and the same email-verdict gating. Nothing about sending changes.
