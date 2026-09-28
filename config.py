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
    GEMINI_API_KEY  - same as OPENAI_API_KEY but uses Google's Gemini through
                      its OpenAI-compatible endpoint instead. Free from Google
                      AI Studio. If both keys are set, Gemini is preferred.
    GEMINI_MODEL    - overrides the default Gemini model name.
    SYSTEM_SMTP_EMAIL / SYSTEM_SMTP_PASSWORD
                    - optional Gmail address + App Password used as a fallback
                      sender for password-reset emails when the user has no
                      connected sender account of their own. Without it, reset
                      emails go out from the user's own sender account.

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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Autopilot AI model. Cheap and fast; the model name is a constant so a
# future swap is one line. Pricing (approx, check openai.com/pricing):
# gpt-4o-mini is ~$0.15 / 1M input tokens and ~$0.60 / 1M output tokens,
# which works out to a fraction of a cent per lead.
AI_MODEL = "gpt-4o-mini"
AI_PRICE_IN_PER_M = 0.15   # USD per million input tokens (approx)
AI_PRICE_OUT_PER_M = 0.60  # USD per million output tokens (approx)

# Gemini model, env-overridable. gemini-2.0-flash is cheap and fast and
# speaks the OpenAI-compatible chat-completions dialect below. Gemini's
# free tier covers this app's volume, so cost is reported as zero.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip() \
    or "gemini-2.0-flash"

# Optional system sender for password-reset emails: a Gmail address + App
# Password used when the resetting user has no connected sender account of
# their own. Without these, reset emails go out from the user's own account.
SYSTEM_SMTP_EMAIL = os.environ.get("SYSTEM_SMTP_EMAIL", "").strip()
SYSTEM_SMTP_PASSWORD = os.environ.get("SYSTEM_SMTP_PASSWORD", "").strip()

# Compliance footer defaults for cold email (CAN-SPAM style: company name +
# physical address + one-click unsubscribe). Per-user values set on the
# Settings page override these; env vars override the built-ins below.
COMPANY_NAME = os.environ.get("COMPANY_NAME", "TechXpert").strip() or "TechXpert"
COMPANY_ADDRESS = os.environ.get(
    "COMPANY_ADDRESS", "REPLACE WITH YOUR BUSINESS ADDRESS").strip() \
    or "REPLACE WITH YOUR BUSINESS ADDRESS"


def ai_provider():
    """Which AI backend to use: 'gemini', 'openai', or None.

    Gemini is preferred when both keys are set, because the user asked
    for it and its free tier covers this volume."""
    if GEMINI_API_KEY:
        return "gemini"
    if OPENAI_API_KEY:
        return "openai"
    return None


def ai_model_name():
    """Display name of the model the Autopilot writer will use."""
    return GEMINI_MODEL if ai_provider() == "gemini" else AI_MODEL


def ai_enabled():
    """Is AI writing available? Never expose the key itself."""
    return ai_provider() is not None

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
