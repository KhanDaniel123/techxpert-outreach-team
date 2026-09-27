"""Environment-based configuration. No config.json on the hosted version.

Required env vars (see README for the deploy guide):
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  - one Google Cloud OAuth client for the app
    APP_URL                                 - public URL, e.g. https://my-app.vercel.app
    DATABASE_URL                            - Postgres connection string (Neon free tier);
                                              unset -> local SQLite fallback for dev
    FERNET_KEY                              - encryption key for Gmail OAuth tokens at rest
    CRON_SECRET                             - shared secret protecting /api/process-queue
    SECRET_KEY                              - Flask session secret (random default is fine for dev)

Optional:
    SENDING_TZ_NOTE - not used in code; sending windows are evaluated in server
                      local time (UTC on Vercel). Documented in README.
"""
import os

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
APP_URL = os.environ.get("APP_URL", "http://localhost:5000").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
FERNET_KEY = os.environ.get("FERNET_KEY", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

LOGIN_REDIRECT_URI = f"{APP_URL}/login/callback"
GMAIL_REDIRECT_URI = f"{APP_URL}/oauth/callback"

OAUTH_READY = bool(GOOGLE_CLIENT_ID) and "YOUR_CLIENT_ID" not in GOOGLE_CLIENT_ID


def check_prod():
    """Return a list of missing settings that matter in production."""
    missing = []
    if not OAUTH_READY:
        missing.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")
    if not DATABASE_URL:
        missing.append("DATABASE_URL (Postgres; without it the app uses throwaway local SQLite)")
    if not FERNET_KEY:
        missing.append("FERNET_KEY (Gmail tokens cannot be encrypted without it)")
    if not CRON_SECRET:
        missing.append("CRON_SECRET (/api/process-queue would reject every call)")
    return missing
