"""One-click unsubscribe for cold outreach.

Every outgoing campaign email carries a signed, opaque unsubscribe link:

    {APP_URL}/unsubscribe?t=<token>

The token is a Fernet-encrypted JSON payload {"lead_id", "user_id", "ts"}
using the same key as the stored Gmail App Passwords (crypto.py). It is
opaque: nothing about the lead leaks into the URL, tokens cannot be forged,
and one lead's token cannot be modified into another lead's (no ID
enumeration). Tokens expire after UNSUB_TOKEN_MAX_AGE_DAYS; a tampered or
expired token shows a safe error page instead of unsubscribing anyone.

Company name + physical address (required on commercial email) are
per-user settings, editable on the Settings page. Until the user replaces
it, the address ships as an obvious placeholder so nobody sends a real
campaign with a fake address unnoticed.
"""
import json
import time

import config
import crypto
import db

# Unsubscribe links stay valid for 2 years: long enough that old emails
# still honor opt-outs, short enough that a leaked DB/key rotation bounds
# exposure. FERNET_KEY rotation invalidates previously issued tokens;
# leads can still be suppressed manually from the campaign page.
UNSUB_TOKEN_MAX_AGE_DAYS = 730

ADDRESS_PLACEHOLDER = "REPLACE WITH YOUR BUSINESS ADDRESS"


def signed_token(lead_id, user_id):
    """Mint an opaque unsubscribe token for a lead."""
    payload = json.dumps({"lead_id": int(lead_id),
                          "user_id": int(user_id),
                          "ts": time.time()})
    return crypto.encrypt_token(payload)


def verify_token(token, max_age_days=UNSUB_TOKEN_MAX_AGE_DAYS):
    """Return {"lead_id", "user_id"} for a valid token, else None.

    Rejects: malformed/forged tokens (Fernet auth fails), non-JSON or
    wrong-shaped payloads, expired tokens, and tokens whose lead row is
    missing or belongs to a different user than the token claims.
    """
    if not token:
        return None
    try:
        payload = json.loads(crypto.decrypt_token(token))
    except Exception:
        return None
    try:
        lead_id = int(payload["lead_id"])
        user_id = int(payload["user_id"])
        ts = float(payload["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    if time.time() - ts > max_age_days * 86400:
        return None
    lead = db.q("SELECT id, user_id FROM leads WHERE id=?", (lead_id,), one=True)
    if not lead or int(lead["user_id"]) != user_id:
        return None
    return {"lead_id": lead_id, "user_id": user_id}


def unsubscribe_url(lead_id, user_id):
    """Full one-click URL for a lead. Also used in List-Unsubscribe."""
    base = (config.APP_URL or "").rstrip("/")
    return f"{base}/unsubscribe?t={signed_token(lead_id, user_id)}"


def get_company_info(user_id):
    """(company_name, address) for a user.

    Per-user Settings page values win; unset fields fall back to the
    COMPANY_NAME / COMPANY_ADDRESS env vars (config.py defaults).
    """
    name = (config.COMPANY_NAME or "").strip() or "TechXpert"
    addr = (config.COMPANY_ADDRESS or "").strip() or ADDRESS_PLACEHOLDER
    try:
        row = db.get_user_settings(user_id)
    except Exception:
        row = None
    if row:
        if (row.get("company_name") or "").strip():
            name = row["company_name"].strip()
        if (row.get("company_address") or "").strip():
            addr = row["company_address"].strip()
    return name, addr


def address_is_placeholder(user_id):
    """True when the user has not set a real business address yet."""
    _, addr = get_company_info(user_id)
    return addr.strip() == ADDRESS_PLACEHOLDER


def build_footer(user_id, lead_id):
    """Plain-text compliance footer appended to every outgoing campaign
    email and follow-up: company name, physical address, one-click
    unsubscribe link."""
    name, addr = get_company_info(user_id)
    link = unsubscribe_url(lead_id, user_id)
    return (f"\n\n-- \n{name}\n{addr}\n"
            f"Unsubscribe: {link}\n")


def mark_unsubscribed(lead_id, user_id):
    """Flag a lead unsubscribed. Returns True if the row was updated."""
    lead = db.q("SELECT id FROM leads WHERE id=? AND user_id=?",
                (lead_id, user_id), one=True)
    if not lead:
        return False
    db.w("UPDATE leads SET unsubscribed=1 WHERE id=?", (lead_id,))
    return True
