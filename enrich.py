"""Website enrichment: extract public emails, phones, and contact-form signals.

Free, no API keys. Only reads publicly listed contact info.
Cannot verify inboxes. Expect ~30-40% email hit rate on small trade sites.
"""
import re
import time
import json
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
TIMEOUT = 20

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

# Image/file extensions that are never emails
BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "pdf"}
# Contact-ish page guesses, tried in order
CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/contact.html", "/contacts"]


def _clean_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _fetch(url):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
            return r.text
    except Exception:
        pass
    return ""


def _extract_emails(html):
    found = []
    for m in EMAIL_RE.findall(html or ""):
        local, _, domain = m.partition("@")
        tld = domain.rsplit(".", 1)[-1].lower()
        if tld in BAD_TLDS:
            continue
        if m.lower() not in ("example@example.com",):
            if m not in found:
                found.append(m)
    return found


def _has_contact_form(soup):
    for form in soup.find_all("form"):
        blob = " ".join([
            form.get("id", ""), form.get("class", [""])[0] if form.get("class") else "",
            form.get("action", ""), form.get_text(" ", strip=True)[:500],
        ]).lower()
        if any(k in blob for k in ("contact", "quote", "estimate", "message", "name", "email")):
            inputs = form.find_all(["input", "textarea", "select"])
            if len(inputs) >= 2:
                return True
    return False


def _jsonld_business(html):
    """Pull name/phone/address from schema.org JSON-LD if present."""
    out = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                t = str(item.get("@type", ""))
                if "Business" in t or t in ("Organization", "LocalBusiness", "Plumber", "Electrician", "HVACBusiness", "HomeAndConstructionBusiness", "ProfessionalService"):
                    out["name"] = item.get("name", "")
                    out["phone"] = item.get("telephone", "")
                    addr = item.get("address", {})
                    if isinstance(addr, dict):
                        out["address"] = ", ".join(x for x in [
                            addr.get("streetAddress"), addr.get("addressLocality"),
                            addr.get("addressRegion"), addr.get("postalCode")] if x)
                    elif isinstance(addr, str):
                        out["address"] = addr
                    return out
    except Exception:
        pass
    return out


def enrich_website(website, pause=1.0):
    """Returns dict: emails, email, has_contact_form, phone, name, address,
    pages_checked, page_text, error. page_text is the homepage's visible
    text (truncated), used for chain/franchise signal detection."""
    result = {"emails": [], "email": "", "has_contact_form": False, "phone": "",
              "name": "", "address": "", "pages_checked": 0, "page_text": "",
              "error": ""}
    base = _clean_url(website)
    if not base:
        result["error"] = "no website"
        return result
    try:
        pages = [base] + [urljoin(base + "/", p.lstrip("/")) for p in CONTACT_PATHS[:3]]
        seen_html = []
        for i, url in enumerate(pages):
            if i > 0:
                time.sleep(pause)
            html = _fetch(url)
            if not html:
                continue
            result["pages_checked"] += 1
            seen_html.append(html)
            if i == 0:
                ld = _jsonld_business(html)
                result["name"] = ld.get("name", "")
                result["phone"] = ld.get("phone", "")
                result["address"] = ld.get("address", "")
        if not seen_html:
            result["error"] = "site unreachable"
            return result
        for html in seen_html:
            for e in _extract_emails(html):
                if e not in result["emails"]:
                    result["emails"].append(e)
        # prefer generic inboxes last; prefer info@/contact@ first is fine either way
        result["email"] = result["emails"][0] if result["emails"] else ""
        soup = BeautifulSoup(seen_html[0], "html.parser")
        result["has_contact_form"] = _has_contact_form(soup)
        # Visible homepage text for chain/franchise signal detection
        # (lead_quality.detect_fit). Scripts/styles stripped, truncated.
        try:
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            result["page_text"] = soup.get_text(" ", strip=True)[:3000]
        except Exception:
            result["page_text"] = ""
        if not result["phone"]:
            phones = PHONE_RE.findall(seen_html[0])
            result["phone"] = phones[0] if phones else ""
        # page title as fallback business name
        if not result["name"] and soup.title:
            result["name"] = soup.title.get_text(" ", strip=True)[:80]
    except Exception as e:
        result["error"] = str(e)[:120]
    return result


def enrich_leads(lead_rows, progress_cb=None, pause=1.0):
    """lead_rows: sqlite Row objects with id + website. Returns list of dicts."""
    out = []
    for i, lead in enumerate(lead_rows):
        r = enrich_website(lead["website"], pause=pause)
        r["lead_id"] = lead["id"]
        out.append(r)
        if progress_cb:
            progress_cb(i + 1, len(lead_rows))
    return out
