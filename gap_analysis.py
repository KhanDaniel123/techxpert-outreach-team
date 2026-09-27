"""Autopilot gap analyzer: deterministic website checks.

For a lead with a website, fetch the site (reusing enrich.py's fetcher) and
run a set of purely observational checks. Every finding is a plain-English
statement of something OBSERVED, never an inference about pain, revenue, or
traffic. The AI writer may only mention these findings.

Findings are phrased as facts ("no booking page found") so the model can
quote them directly as the email hook.
"""
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import enrich as enrichmod

# words that suggest a visitor can book / request a quote
BOOKING_WORDS = ("book", "booking", "schedule", "scheduling", "appointment",
                 "appointments", "quote", "quotes", "estimate", "estimates",
                 "get a quote", "request a quote")
SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "linkedin.com",
                  "twitter.com", "x.com", "youtube.com", "tiktok.com")
SLOW_SECONDS = 4.0


def _text_and_links(html):
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    hrefs = " ".join(a.get("href", "") for a in soup.find_all("a", href=True)).lower()
    return soup, text, hrefs


def analyze_website(website, fetch_fn=None):
    """Return {"findings": [...], "reachable": bool, "load_seconds": float}.

    fetch_fn(url) -> html string; defaults to enrich's fetcher. Injected in
    tests so no network is needed.
    """
    findings = []
    website = (website or "").strip()
    if not website:
        return {"findings": ["the business has no website"],
                "reachable": False, "load_seconds": 0.0}

    fetch = fetch_fn or enrichmod._fetch
    url = enrichmod._clean_url(website)
    t0 = time.time()
    try:
        html = fetch(url) or ""
    except Exception:
        html = ""
    load_s = round(time.time() - t0, 1)

    if not html:
        return {"findings": ["the business website could not be reached"],
                "reachable": False, "load_seconds": load_s}

    soup, text, hrefs = _text_and_links(html)
    netloc = urlparse(url).netloc.lower()

    # contact email: any address on the page that is not on the site's own
    # domain is still a listed email; keep it simple and factual.
    emails = [e for e in enrichmod._extract_emails(html)]
    if emails:
        findings.append("the website lists a contact email address")
    else:
        findings.append("no contact email found on the website")

    phones = enrichmod.PHONE_RE.findall(soup.get_text(" ", strip=True))
    if phones:
        findings.append("the website lists a phone number")
    else:
        findings.append("no phone number found on the website")

    if any(w in text for w in BOOKING_WORDS):
        findings.append("the website has a booking, scheduling, or quote option")
    else:
        findings.append("no booking, scheduling, or quote option found on the website")

    if "testimonial" in text or "review" in text:
        findings.append("the website shows reviews or testimonials")
    else:
        findings.append("no reviews or testimonials found on the website")

    if any(d in hrefs for d in SOCIAL_DOMAINS):
        findings.append("the website links to social media profiles")
    else:
        findings.append("no social media links found on the website")

    viewport = soup.find("meta", attrs={"name": "viewport"})
    if viewport:
        findings.append("the website is set up for mobile screens")
    else:
        findings.append("the website has no mobile layout tag (may look broken on phones)")

    if load_s >= SLOW_SECONDS:
        findings.append(f"the website took {load_s}s to load (slow)")

    return {"findings": findings, "reachable": True, "load_seconds": load_s}
