"""Lead ingestion: CSV import + manual add. (Same as local app, user-scoped.)"""
import csv
import io
import time

import db

CSV_COLUMNS = ["business_name", "address", "phone", "website", "email",
               "rating", "review_count", "category", "notes", "personalized_line"]


def import_csv(user_id, campaign_id, file_stream, source="csv"):
    """file_stream: binary file-like. Returns (imported_count, errors)."""
    raw = file_stream.read()
    text = raw.decode("utf-8-sig", errors="ignore") if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text))
    imported, errors = 0, []
    for i, row in enumerate(reader, start=2):
        try:
            name = (row.get("business_name") or row.get("name") or "").strip()
            website = (row.get("website") or "").strip()
            if not name and not website:
                continue
            db.w(
                """INSERT INTO leads (user_id, campaign_id, business_name, address, phone,
                   website, email, rating, review_count, category, source, notes,
                   personalized_line, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, campaign_id, name,
                 (row.get("address") or "").strip(), (row.get("phone") or "").strip(),
                 website, (row.get("email") or "").strip(),
                 (row.get("rating") or "").strip(), (row.get("review_count") or "").strip(),
                 (row.get("category") or "").strip(), source,
                 (row.get("notes") or "").strip(),
                 (row.get("personalized_line") or "").strip(), time.time()))
            imported += 1
        except Exception as e:
            errors.append(f"row {i}: {e}")
    return imported, errors


def add_manual(user_id, campaign_id, data):
    db.w(
        """INSERT INTO leads (user_id, campaign_id, business_name, address, phone,
           website, email, rating, review_count, category, source, notes,
           personalized_line, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, campaign_id, data.get("business_name", ""), data.get("address", ""),
         data.get("phone", ""), data.get("website", ""), data.get("email", ""),
         data.get("rating", ""), data.get("review_count", ""), data.get("category", ""),
         "manual", data.get("notes", ""), data.get("personalized_line", ""), time.time()))


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
