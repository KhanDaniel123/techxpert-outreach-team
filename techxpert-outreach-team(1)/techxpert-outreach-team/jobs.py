"""Chunked background jobs for serverless.

The local app ran websearch/enrich/validate as threads. On Vercel there are
no long-lived threads, so each job is split into small chunks. Every hit of
POST /api/process-queue (cron) runs ONE chunk of the oldest running job, then
returns. Progress is visible on the campaign page via /job/<id>/status.

Job cursor state lives in jobs.payload (JSON).
"""
import json
import time

import db
import pipeline as pipelinemod

WEBSEARCH_FETCH_PER_CHUNK = 3
ENRICH_PER_CHUNK = 2          # each site fetch + email validation can take ~10-40s
VALIDATE_PER_CHUNK = 10
AUTOPILOT_PER_CHUNK = 1       # one lead per tick: site fetch + 2 AI calls can take ~30-60s


def start_job(user_id, campaign_id, kind):
    return db.w(
        """INSERT INTO jobs (user_id, campaign_id, kind, status, total, done,
           result, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (user_id, campaign_id, kind, "running", 0, 0, "", "{}", time.time()))


def process_one_job_chunk():
    """Run a single chunk of the oldest running job. Returns job id or None."""
    job = db.q("SELECT * FROM jobs WHERE status='running' ORDER BY id LIMIT 1", one=True)
    if not job:
        return None
    handler = {"websearch": _websearch_chunk,
               "enrich": _enrich_chunk,
               "validate": _validate_chunk,
               "autopilot": _autopilot_chunk}.get(job["kind"])
    if not handler:
        db.w("UPDATE jobs SET status='failed', result=? WHERE id=?",
             (f"unknown job kind {job['kind']}", job["id"]))
        return job["id"]
    try:
        handler(job)
    except Exception as e:
        db.w("UPDATE jobs SET status='failed', result=? WHERE id=?",
             (str(e)[:300], job["id"]))
    return job["id"]


def _payload(job):
    try:
        return json.loads(job["payload"] or "{}")
    except Exception:
        return {}


def _save(job_id, payload=None, **fields):
    sets, args = [], []
    if payload is not None:
        sets.append("payload=?")
        args.append(json.dumps(payload))
    for k, v in fields.items():
        sets.append(f"{k}=?")
        args.append(v)
    args.append(job_id)
    db.w(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", tuple(args))


# ---------------- websearch ----------------

def _websearch_chunk(job):
    from scrapers import websearch
    from urllib.parse import urlparse
    p = _payload(job)
    if p.get("phase") != "fetch":
        # phase 1: fetch result links once
        camp = db.q("SELECT * FROM campaigns WHERE id=?", (job["campaign_id"],), one=True)
        links = websearch.ddg_links(f"{camp['niche']} {camp['location']}".strip())
        if not links:
            time.sleep(5)
            links = websearch.ddg_links(f"{camp['niche']} {camp['location']}".strip())
        seen, urls = set(), []
        for u in links:
            if not pipelinemod.is_aggregator_domain(urlparse(u).netloc):
                dom = urlparse(u).netloc
                if dom not in seen:
                    seen.add(dom)
                    urls.append(u)
        _save(job["id"], {"phase": "fetch", "urls": urls, "idx": 0,
                          "query": f"{camp['niche']} {camp['location']}".strip()},
              total=len(urls), done=0,
              result=f"{len(links)} raw links, {len(urls)} business domains queued")
        return
    # phase 2: fetch site identities in small chunks
    urls, idx = p["urls"], p["idx"]
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (job["campaign_id"],), one=True)
    added = 0
    skipped = 0
    niche = (camp["niche"] if camp else "") or ""
    location = (camp["location"] if camp else "") or ""
    for url in urls[idx:idx + WEBSEARCH_FETCH_PER_CHUNK]:
        ident = websearch._site_identity(url)
        name = (ident.get("name") or "").strip()
        if name and websearch.ARTICLE_TITLE_RE.search(name):
            pipelinemod.log_skipped(url, name, "article/ranking title (ARTICLE_TITLE_RE)")
            skipped += 1
            continue
        keep, reason = pipelinemod.discovery_verdict(name, url, niche, location)
        if not keep:
            pipelinemod.log_skipped(url, name, reason)
            skipped += 1
            continue
        if not name:
            name = urlparse(url).netloc.replace("www.", "")
        site = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        dup = db.q("SELECT id FROM leads WHERE campaign_id=? AND website=?",
                   (job["campaign_id"], site), one=True)
        if not dup:
            db.w(
                """INSERT INTO leads (user_id, campaign_id, business_name, address, phone,
                   website, email, rating, review_count, category, source, notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job["user_id"], job["campaign_id"], name, ident.get("address", ""),
                 ident.get("phone", ""), site, "", "", "", camp["niche"] if camp else "",
                 "websearch", f"found via web search: {p.get('query', '')}", time.time()))
            added += 1
    idx += WEBSEARCH_FETCH_PER_CHUNK
    p["idx"] = idx
    if idx >= len(urls):
        _save(job["id"], p, status="done", done=len(urls),
              result=f"web search '{p.get('query', '')}': {len(urls)} domains checked, "
                     f"{added} new leads this run, {skipped} junk results skipped")
    else:
        _save(job["id"], p, done=min(idx, len(urls)))


# ---------------- enrich ----------------

def _validate_one_email(email):
    try:
        import email_validator as ev
        r = ev.validate_email(email)
        detail = r["reason"]
        if r["mx_host"]:
            detail += f" [{r['mx_host']}]"
        return r["verdict"], detail[:200]
    except Exception as e:
        return "unknown", f"validator error: {str(e)[:120]}"


def _enrich_chunk(job):
    import enrich as enrichmod
    if not _payload(job).get("init"):
        total = db.q(
            "SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND website<>'' AND email=''",
            (job["campaign_id"],), one=True)["c"]
        _save(job["id"], {"init": True}, total=total, done=0)
        if total == 0:
            _save(job["id"], {"init": True}, status="done",
                  result="nothing to enrich (no unleaded websites)")
            return
    rows = db.q(
        "SELECT id, website FROM leads WHERE campaign_id=? AND website<>'' AND email='' "
        "ORDER BY id LIMIT ?",
        (job["campaign_id"], ENRICH_PER_CHUNK))
    if not rows:
        _save(job["id"], status="done", result="enrichment complete")
        return
    results = enrichmod.enrich_leads(rows, pause=0.8)
    updated = 0
    for res in results:
        if res["email"]:
            verdict, detail = _validate_one_email(res["email"])
            db.w("""UPDATE leads SET email=?, email_verdict=?, email_verdict_detail=?,
                    has_contact_form=?, phone=COALESCE(NULLIF(phone,''), ?), notes=? WHERE id=?""",
                 (res["email"], verdict, detail,
                  1 if res["has_contact_form"] else 0,
                  res["phone"] or "", f"enriched {res['pages_checked']} pages", res["lead_id"]))
            updated += 1
        elif res["has_contact_form"]:
            db.w("UPDATE leads SET has_contact_form=1, notes='contact form, no public email' WHERE id=?",
                 (res["lead_id"],))
    done = db.q("SELECT done FROM jobs WHERE id=?", (job["id"],), one=True)["done"] or 0
    _save(job["id"], done=done + len(rows),
          result=f"{updated} new emails so far")


# ---------------- autopilot (AI email writing) ----------------

def _autopilot_chunk(job):
    """Write AI emails for selected leads with an email address, one lead per
    chunk (site fetch + 2 AI calls can take ~30-60s, so never more). Cached
    leads are skipped, never regenerated. Runs only while the campaign's
    autopilot toggle is on."""
    import ai_writer as aimod
    camp = db.q("SELECT * FROM campaigns WHERE id=?", (job["campaign_id"],), one=True)
    if not camp or not camp.get("autopilot"):
        _save(job["id"], status="done",
              result="autopilot is off for this campaign; nothing written")
        return
    p = _payload(job)
    if not p.get("init"):
        total = db.q(
            "SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND selected=1 AND email<>''",
            (job["campaign_id"],), one=True)["c"]
        p = {"init": True, "last_id": 0}
        _save(job["id"], p, total=total, done=0, result="AI writing queued")
        db.w("UPDATE leads SET ai_status='pending' WHERE campaign_id=? AND selected=1 "
             "AND email<>'' AND (ai_status IS NULL OR ai_status='')",
             (job["campaign_id"],))
        if total == 0:
            _save(job["id"], p, status="done", result="no leads to write for")
        return
    rows = db.q(
        "SELECT * FROM leads WHERE campaign_id=? AND selected=1 AND email<>'' AND id>? "
        "ORDER BY id LIMIT ?",
        (job["campaign_id"], p.get("last_id", 0), AUTOPILOT_PER_CHUNK))
    if not rows:
        _save(job["id"], status="done", result="AI writing complete")
        return
    for lead in rows:
        if not aimod.get_ai_content(lead["id"]):
            aimod.generate_and_store(lead, camp)
    p["last_id"] = rows[-1]["id"]
    done = db.q("SELECT done FROM jobs WHERE id=?", (job["id"],), one=True)["done"] or 0
    ready = db.q("SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND ai_status='ready'",
                 (job["campaign_id"],), one=True)["c"]
    _save(job["id"], p, done=done + len(rows),
          result=f"{ready} AI emails written so far")

# ---------------- validate ----------------

def _validate_chunk(job):
    from email_validator import validate_emails
    p = _payload(job)
    if not p.get("init"):
        total = db.q(
            "SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND email<>''",
            (job["campaign_id"],), one=True)["c"]
        p = {"init": True, "last_id": 0}
        _save(job["id"], p, total=total, done=0)
        if total == 0:
            _save(job["id"], p, status="done", result="no emails to validate")
            return
    rows = db.q(
        "SELECT id, email FROM leads WHERE campaign_id=? AND email<>'' AND id>? "
        "ORDER BY id LIMIT ?",
        (job["campaign_id"], p.get("last_id", 0), VALIDATE_PER_CHUNK))
    batch = rows
    if not batch:
        _save(job["id"], status="done", result="validation complete")
        return
    results = validate_emails([r["email"] for r in batch], max_workers=5)
    counts = {"valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
    for row, res in zip(batch, results):
        detail = res["reason"]
        if res["mx_host"]:
            detail += f" [{res['mx_host']}]"
        db.w("UPDATE leads SET email_verdict=?, email_verdict_detail=? WHERE id=?",
             (res["verdict"], detail[:200], row["id"]))
        counts[res["verdict"]] = counts.get(res["verdict"], 0) + 1
    p["last_id"] = batch[-1]["id"]
    done = db.q("SELECT done FROM jobs WHERE id=?", (job["id"],), one=True)["done"] or 0
    _save(job["id"], p, done=done + len(batch),
          result="validated %d so far (%s)" % (done + len(batch),
                 ", ".join(f"{k}={v}" for k, v in counts.items() if v)))
