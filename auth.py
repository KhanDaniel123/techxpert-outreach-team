"""Built-in email + password authentication (no Google Sign-In).

Registration is open: anyone with the app URL can create an account.
This is a private team-tool URL, so that is the intended access model.
Passwords are hashed with werkzeug PBKDF2; plaintext passwords never
touch the database and are never logged.

Duplicate emails are rejected at registration (app-level check plus a
UNIQUE constraint added by db._ensure_app_users_email_unique). Databases
that already contain legacy duplicate rows are handled gracefully: login
and password reset resolve to the data-bearing (or oldest) account, and
the constraint migration waits until the dupes are gone.

Password reset: single-use signed tokens (secrets.token_urlsafe), stored
as SHA-256 hashes, 1-hour expiry, max 3 requests per email per hour. The
reset email is sent from the user's own connected Gmail sender account,
falling back to an optional system sender (SYSTEM_SMTP_EMAIL /
SYSTEM_SMTP_PASSWORD in config). Public messages never reveal whether an
email address has an account.
"""
import hashlib
import re
import secrets
import time

from werkzeug.security import generate_password_hash, check_password_hash

import db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8

RESET_TOKEN_TTL_SECONDS = 3600
RESET_MAX_REQUESTS_PER_HOUR = 3
RESET_GENERIC_MESSAGE = ("If an account exists with that email, "
                         "a password reset link has been sent.")


def _clean_email(email):
    return (email or "").strip().lower()


def _account_has_data(user_id):
    """True when the account owns any campaigns, sender accounts or leads."""
    for table in ("campaigns", "sender_accounts", "leads"):
        try:
            row = db.q("SELECT COUNT(*) c FROM %s WHERE user_id=?" % table,
                       (user_id,), one=True)
            if row and row["c"]:
                return True
        except Exception:
            continue
    return False


def primary_user_for_email(email):
    """Among all accounts sharing one email (legacy duplicates), return the
    account login and password reset should use: the one that has data
    first, then the oldest. Returns None when no account exists."""
    users = db.get_users_by_email(_clean_email(email))
    if not users:
        return None
    users.sort(key=lambda u: (0 if _account_has_data(u["id"]) else 1,
                              u["id"]))
    return users[0]


def register_user(email, name, password):
    """Create a new user. Returns (ok: bool, payload: dict|str error)."""
    email = _clean_email(email)
    name = (name or "").strip() or email.split("@")[0]
    if not EMAIL_RE.match(email):
        return False, "Enter a valid email address."
    if not password or len(password) < MIN_PASSWORD_LEN:
        return False, f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if db.get_users_by_email(email):
        return False, "An account with this email already exists. Please log in."
    try:
        user = db.create_user(email, name, generate_password_hash(password))
    except Exception:
        # UNIQUE constraint race (two simultaneous signups): treat as dup.
        return False, "An account with this email already exists. Please log in."
    return True, user


def verify_login(email, password):
    """Return the user dict when credentials are valid, else None.

    With legacy duplicate rows, candidates are tried data-bearing-first,
    oldest-first, so the user always lands in the account that holds
    their campaigns instead of an empty duplicate.
    """
    email = _clean_email(email)
    users = db.get_users_by_email(email)
    users.sort(key=lambda u: (0 if _account_has_data(u["id"]) else 1,
                              u["id"]))
    for user in users:
        try:
            if check_password_hash(user["password_hash"], password or ""):
                return user
        except Exception:
            continue
    return None


def _issue_reset_token(email):
    """Create a password-reset token row. Returns (user, token).

    Returns (None, None) when the caller must show the generic message
    (bad email format, rate-limited, or unknown account) so that account
    existence is never revealed. Token scheme: secrets.token_urlsafe(32),
    SHA-256 hash stored, 1-hour expiry, single-use.
    """
    email = _clean_email(email)
    if not EMAIL_RE.match(email):
        return None, None
    cutoff = time.time() - 3600
    recent = db.q("SELECT COUNT(*) c FROM password_resets "
                  "WHERE email=? AND created_at>?", (email, cutoff), one=True)
    if recent and recent["c"] >= RESET_MAX_REQUESTS_PER_HOUR:
        return None, None
    user = primary_user_for_email(email)
    if not user:
        return None, None
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    db.w("INSERT INTO password_resets "
         "(user_id, email, token_hash, created_at, expires_at, used_at) "
         "VALUES (?,?,?,?,?,NULL)",
         (user["id"], email, token_hash, now, now + RESET_TOKEN_TTL_SECONDS))
    return user, token


def request_password_reset(email):
    """Start a password reset. Returns (ok, message).

    The public message is always generic so it never reveals whether the
    email has an account. Returns ok=False only when the reset email
    genuinely cannot be sent (no mail sender available).
    """
    user, token = _issue_reset_token(email)
    if not user:
        return True, RESET_GENERIC_MESSAGE
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        _send_reset_email(user, token)
    except Exception:
        db.w("DELETE FROM password_resets WHERE token_hash=?", (token_hash,))
        return False, ("We couldn't send the reset email because no mail "
                       "sender is available. Please contact your administrator.")
    return True, RESET_GENERIC_MESSAGE


def issue_reset_token_for_display(email):
    """Create a reset token for on-screen display instead of email.

    Identical token scheme, TTL, rate limit, and single-use semantics to
    request_password_reset; only the delivery channel differs. The caller
    MUST use this only when the requester holds a valid authenticated
    session.

    Security rationale: signup now rejects already-registered emails, so
    no NEW duplicate accounts can be created; the only sessions that can
    reach this fallback belong to pre-existing account holders (for
    example a user locked out of their original account while logged in
    on an older duplicate). The token keeps every existing protection:
    unpredictable (secrets.token_urlsafe(32)), stored as a SHA-256 hash,
    1-hour expiry, single-use, max 3 per hour per email.

    Returns (token, user); (None, None) when the generic message applies.
    """
    user, token = _issue_reset_token(email)
    if not user:
        return None, None
    return token, user


def redeem_password_reset(token, new_password):
    """Consume a reset token and set a new password. Single-use."""
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        return False, f"Password must be at least {MIN_PASSWORD_LEN} characters."
    token_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    row = db.q("SELECT * FROM password_resets WHERE token_hash=?",
               (token_hash,), one=True)
    if not row:
        return False, "This reset link is invalid or has expired."
    if row["used_at"]:
        return False, "This reset link has already been used."
    if row["expires_at"] < time.time():
        return False, "This reset link has expired."
    db.w("UPDATE app_users SET password_hash=? WHERE id=?",
         (generate_password_hash(new_password), row["user_id"]))
    now = time.time()
    db.w("UPDATE password_resets SET used_at=? WHERE id=?", (now, row["id"]))
    # Invalidate any other outstanding tokens for this user.
    db.w("UPDATE password_resets SET used_at=? "
         "WHERE user_id=? AND used_at IS NULL", (now, row["user_id"]))
    return True, "Password updated. Please log in."


def _send_reset_email(user, token):
    """Send the reset link from the user's own sender account, else the
    optional system sender. Raises when no mail path exists."""
    import config
    import smtp_mail
    reset_url = config.APP_URL.rstrip("/") + "/reset-password/" + token
    subject = "Reset your TechXpert Outreach password"
    body = ("Hi %s,\n\n"
            "Someone requested a password reset for your TechXpert Outreach "
            "account.\n\n"
            "Reset your password within 1 hour:\n%s\n\n"
            "If you didn't ask for this, just ignore this email.\n"
            % ((user.get("name") or "there"), reset_url))
    to_addr = user["email"]
    errors = []
    accts = db.q("SELECT * FROM sender_accounts "
                 "WHERE user_id=? AND status='active' ORDER BY id",
                 (user["id"],))
    for acct in accts or []:
        try:
            smtp_mail.send_message(acct, to_addr, subject, body)
            return True
        except Exception as e:
            errors.append(str(e)[:120])
    sys_email = (getattr(config, "SYSTEM_SMTP_EMAIL", "") or "").strip()
    sys_pw = (getattr(config, "SYSTEM_SMTP_PASSWORD", "") or "").strip()
    if sys_email and sys_pw:
        import crypto
        sys_acct = {"email": sys_email,
                    "password_enc": crypto.encrypt_token(sys_pw)}
        smtp_mail.send_message(sys_acct, to_addr, subject, body)
        return True
    raise RuntimeError("no mail sender available"
                       + (": " + "; ".join(errors) if errors else ""))
