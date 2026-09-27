"""Environment-based configuration. No config.json on the hosted version.

Required env vars (see README for the deploy guide):
    APP_URL       - public URL, e.g. https://my-app.vercel.app
    DATABASE_URL  - Postgres connection string (Neon free tier);
                    unset -> local SQLite fallback for dev
    FERNET_KEY    - encryption key for stored Gmail App Passwords at rest.
                    ALSO used as the Flask session secret key: it is stable
                    across serverless invocations, which a random key would not be.
    CRON_SECRET   - shared secret protecting /api/process-queue

Optional:
    SENDING_TZ_NOTE - not used in code; sending windows are evaluated in server
                      local time (UTC on Vercel). Documented in README.
    OPENAI_API_KEY  - enables Autopilot (AI-written emails). Without it the
                      autopilot toggle is hidden and the app works exactly as
                      before. The key is never displayed in the UI.

There is deliberately NO Google OAuth here. Login is built-in
email + password (auth.py); sending is direct Gmail SMTP with per-user
App Passwords (smtp_mail.py). No Google Cloud project needed.
"""
import hashlib
import os

APP_URL = os.environ.get("APP_URL", "http://localhost:5000").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
FERNET_KEY = os.environ.get("FERNET_KEY", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# Autopilot AI model. Cheap and fast; the model name is a constant so a
# future swap is one line. Pricing (approx, check openai.com/pricing):
# gpt-4o-mini is ~$0.15 / 1M input tokens and ~$0.60 / 1M output tokens,
# which works out to a fraction of a cent per lead.
AI_MODEL = "gpt-4o-mini"
AI_PRICE_IN_PER_M = 0.15   # USD per million input tokens (approx)
AI_PRICE_OUT_PER_M = 0.60  # USD per million output tokens (approx)


def ai_enabled():
    """Is AI writing available? Never expose the key itself."""
    return bool(OPENAI_API_KEY)

# Stable session secret derived from FERNET_KEY so logins survive across
# serverless invocations (a random key would log everyone out on each cold start).
if FERNET_KEY:
    SECRET_KEY = hashlib.sha256(b"session:" + FERNET_KEY.strip().encode()).hexdigest()
else:
    SECRET_KEY = "dev-secret-change-me"  # local dev only


def check_prod():
    """Return a list of missing settings that matter in production."""
    missing = []
    if not DATABASE_URL:
        missing.append("DATABASE_URL (Postgres; without it the app uses throwaway local SQLite)")
    if not FERNET_KEY:
        missing.append("FERNET_KEY (App Passwords cannot be encrypted without it; sessions also unstable)")
    if not CRON_SECRET:
        missing.append("CRON_SECRET (/api/process-queue would reject every call)")
    return missing
