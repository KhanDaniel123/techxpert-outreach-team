"""Web-search lead discovery (DuckDuckGo HTML endpoint).

Free, no API key, works over plain requests. Google/Bing zero out results
for datacenter IPs and Google Maps needs a real browser (blocked in this
sandbox); DDG's HTML endpoint still serves real organic results.

Flow: query "{niche} {location}" -> result links -> drop directories/social ->
fetch each business homepage -> business name/phone/address from JSON-LD or
<title> -> lead dicts. Enrichment (enrich.py) then finds emails.

Honest limits: DDG sometimes serves thin/bot-check pages (returns 0 links);
result counts vary per query; keep max_leads small (10-25) and retry later
if a query comes back empty. Polite delays between fetches.
"""
import re
import time
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

DIRECTORY_DOMAINS = [
    "yelp.", "angi.", "thumbtack.", "homeadvisor.", "bbb.org", "facebook.",
    "yellowpages.", "consumeraffairs.", "chamberofcommerce.", "mapquest.",
    "manta.", "superpages.", "dexknows.", "houzz.", "porch.", "expertise.",
    "trustpilot.", "google.", "instagram.", "linkedin.", "twitter.", "youtube.",
    "wikipedia.", "mysanantonio.", "local.yahoo.", "forbes.", "bobvila.",
    "thisoldhouse.", "familyhandyman.", "angi.com",
]

# Titles that look like articles/rankings rather than a business homepage
ARTICLE_TITLE_RE = re.compile(
    r"\b(best|top\s+\d+|review[s]?|\d+\s+best)\b.{0,40}\b(compan|contractor|service|plumber|electrician|hvac)\b",
    re.I)


def _is_business_domain(netloc):
    d = (netloc or "").lower()
    if not d or "duckduckgo" in d:
        return False
    return not any(x in d for x in DIRECTORY_DOMAINS)


def ddg_links(query, timeout=30):
    """Raw result URLs from DDG HTML endpoint."""
    r = requests.post("https://html.duckduckgo.com/html/",
                      data={"q": query}, headers=UA, timeout=timeout)
    html = r.text
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    out = []
    for l in links:
        m = re.search(r"uddg=([^&]+)", l)
        u = unquote(m.group(1)) if m else l
        if u.startswith("http") and u not in out:
            out.append(u)
    return out


def _site_identity(url, timeout=20):
    """Fetch homepage -> (name, phone, address) via JSON-LD, title fallback."""
    import enrich as _enrich  # local import to avoid cycles
    html = _enrich._fetch(url)
    if not html:
        return {}
    ld = _enrich._jsonld_business(html)
    if not ld.get("name"):
        try:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title:
                t = soup.title.get_text(" ", strip=True)
                # strip common suffixes: " | Best HVAC in Phoenix"
                ld["name"] = re.split(r"\s[|\-–]\s", t)[0][:80]
        except Exception:
            pass
    return ld


def search_leads(niche, location, max_leads=20, pause=1.5, progress_cb=None):
    """Returns (leads, meta). leads: list of dicts ready for the leads table."""
    query = f"{niche} {location}".strip()
    links = ddg_links(query)
    if not links:  # DDG sometimes serves a thin page; one retry
        time.sleep(5)
        links = ddg_links(query)
    meta = {"query": query, "raw_links": len(links)}
    biz_urls = []
    for u in links:
        if _is_business_domain(urlparse(u).netloc):
            # one page per domain
            dom = urlparse(u).netloc
            if dom not in [urlparse(x).netloc for x in biz_urls]:
                biz_urls.append(u)
        if len(biz_urls) >= max_leads:
            break
    meta["business_domains"] = len(biz_urls)
    leads = []
    for i, url in enumerate(biz_urls):
        if i > 0:
            time.sleep(pause)
        ident = _site_identity(url)
        name = ident.get("name", "").strip()
        if name and ARTICLE_TITLE_RE.search(name):
            if progress_cb:
                progress_cb(i + 1, len(biz_urls))
            continue  # article/ranking page, not a business
        if not name:
            # fall back to domain as placeholder name; user can edit
            name = urlparse(url).netloc.replace("www.", "")
        leads.append({
            "business_name": name,
            "address": ident.get("address", ""),
            "phone": ident.get("phone", ""),
            "website": f"{urlparse(url).scheme}://{urlparse(url).netloc}",
            "email": "", "rating": "", "review_count": "",
            "category": niche, "source": "websearch",
            "notes": f"found via web search: {query}",
        })
        if progress_cb:
            progress_cb(i + 1, len(biz_urls))
    meta["leads"] = len(leads)
    return leads, meta
