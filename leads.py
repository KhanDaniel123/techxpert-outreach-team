"""Lead ingestion: CSV import + manual add. (Same as local app, user-scoped.)"""
import csv
import io
import time

import db

CSV_COLUMNS = ["business_name", "address", "phone", "website", "email",
               "rating", "review_count", "category", "notes", "personalized_line"]


def import_csv(user_id, campaign_id, file_stream, source="csv"):
    """file_stream: binary file-like. Returns (imported_count, merged_count, errors).

    Rows matching an existing lead (same normalized domain or business name)
    are merged into that lead instead of creating a duplicate row; the merge
    only fills empty fields and never overwrites verified data."""
    import lead_quality as lq
    raw = file_stream.read()
    text = raw.decode("utf-8-sig", errors="ignore") if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text))
    camp = db.q("SELECT niche FROM campaigns WHERE id=?", (campaign_id,), one=True)
    niche = (camp["niche"] if camp else "") or ""
    imported, merged, errors = 0, 0, []
    for i, row in enumerate(reader, start=2):
        try:
            name = (row.get("business_name") or row.get("name") or "").strip()
            website = (row.get("website") or "").strip()
            if not name and not website:
                continue
            dup = lq.find_duplicate_lead(campaign_id, name, website)
            if dup:
                # Near-duplicate of an existing lead: fill its gaps, no new row.
                if lq.merge_lead_fields(
                        dup["id"], name=name,
                        address=(row.get("address") or "").strip(),
                        phone=(row.get("phone") or "").strip(),
                        website=website, email=(row.get("email") or "").strip(),
                        rating=(row.get("rating") or "").strip(),
                        review_count=(row.get("review_count") or "").strip()):
                    merged += 1
                continue
            db.w(
                """INSERT INTO leads (user_id, campaign_id, business_name, address, phone,
                   website, email, rating, review_count, category, source, notes,
                   personalized_line, fit, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, campaign_id, name,
                 (row.get("address") or "").strip(), (row.get("phone") or "").strip(),
                 website, (row.get("email") or "").strip(),
                 (row.get("rating") or "").strip(), (row.get("review_count") or "").strip(),
                 (row.get("category") or "").strip(), source,
                 (row.get("notes") or "").strip(),
                 (row.get("personalized_line") or "").strip(),
                 lq.detect_fit(name, niche), time.time()))
            imported += 1
        except Exception as e:
            errors.append(f"row {i}: {e}")
    return imported, merged, errors


def add_manual(user_id, campaign_id, data):
    """Manual lead add. Merges into an existing near-duplicate lead
    (same normalized domain or business name) instead of creating a new row."""
    import lead_quality as lq
    name = (data.get("business_name") or "").strip()
    website = (data.get("website") or "").strip()
    dup = lq.find_duplicate_lead(campaign_id, name, website)
    if dup:
        lq.merge_lead_fields(
            dup["id"], name=name,
            address=(data.get("address") or "").strip(),
            phone=(data.get("phone") or "").strip(),
            website=website, email=(data.get("email") or "").strip(),
            rating=(data.get("rating") or "").strip(),
            review_count=(data.get("review_count") or "").strip())
        return
    camp = db.q("SELECT niche FROM campaigns WHERE id=?", (campaign_id,), one=True)
    niche = (camp["niche"] if camp else "") or ""
    db.w(
        """INSERT INTO leads (user_id, campaign_id, business_name, address, phone,
           website, email, rating, review_count, category, source, notes,
           personalized_line, fit, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, campaign_id, name, data.get("address", ""),
         data.get("phone", ""), website, data.get("email", ""),
         data.get("rating", ""), data.get("review_count", ""),
         data.get("category", ""),
         "manual", data.get("notes", ""), data.get("personalized_line", ""),
         lq.detect_fit(name, niche), time.time()))


SAMPLE_CSV = """business_name,address,phone,website,category,notes,personalized_line
George Brazil Plumbing & Electrical,Phoenix AZ,,https://georgebrazil.com,Plumber,large local shop,"noticed your 4.8-star reviews mention same-day service - impressive for a shop your size"
Parker & Sons,Phoenix AZ,,https://parkerandsons.com,HVAC,,
AC by J,Phoenix AZ,,https://acbyj.com,HVAC,,
Day & Night Air Conditioning,Phoenix AZ,,https://dayandnightair.com,HVAC,,
Howard Air,Phoenix AZ,,https://howardair.com,HVAC,,
Chas Roberts Air Conditioning,Phoenix AZ,,https://chasroberts.com,HVAC,,
Forrest Anderson Plumbing & AC,Phoenix AZ,,https://forrestanderson.net,Plumber,,
Rainforest Plumbing & Air,Phoenix AZ,,https://rainforestplumbing.com,Plumber,,
Arizona's Dukes of Air,Phoenix AZ,,https://azdukesofair.com,HVAC,,
Collins Comfort Masters,Phoenix AZ,,https://collinscomfortmasters.com,HVAC,,
Donley Service Center,Phoenix AZ,,https://donleyservice.com,HVAC,,
Wolff Mechanical,Phoenix AZ,,https://wolffmechanical.com,HVAC,,
"""
