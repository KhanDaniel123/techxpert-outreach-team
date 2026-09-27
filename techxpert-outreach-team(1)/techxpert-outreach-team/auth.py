"""Built-in email + password authentication (no Google Sign-In).

Registration is open: anyone with the app URL can create an account.
This is a private team-tool URL, so that is the intended access model.
Passwords are hashed with werkzeug PBKDF2; plaintext passwords never
touch the database and are never logged.
"""
import re

from werkzeug.security import generate_password_hash, check_password_hash

import db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _clean_email(email):
    return (email or "").strip().lower()


def register_user(email, name, password):
    """Create a new user. Returns (ok: bool, payload: dict|str error)."""
    email = _clean_email(email)
    name = (name or "").strip() or email.split("@")[0]
    if not EMAIL_RE.match(email):
        return False, "Enter a valid email address."
    if not password or len(password) < MIN_PASSWORD_LEN:
        return False, f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if db.get_user_by_email(email):
        return False, "An account with that email already exists. Try logging in."
    user = db.create_user(email, name, generate_password_hash(password))
    return True, user


def verify_login(email, password):
    """Return the user dict when credentials are valid, else None."""
    user = db.get_user_by_email(_clean_email(email))
    if not user:
        return None
    try:
        if check_password_hash(user["password_hash"], password or ""):
            return user
    except Exception:
        pass
    return None
