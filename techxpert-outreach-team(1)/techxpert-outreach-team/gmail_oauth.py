"""Gmail OAuth 2.0 web flow + send + bounce scanning (hosted team version).

Scopes: gmail.send (send mail), gmail.readonly (scan for bounce notifications).
Client ID/secret come from env vars (config.py). OAuth tokens are
Fernet-encrypted at rest via crypto.py and never logged.

NOTE on Google verification: gmail.send is a *restricted* scope. If team
members connect personal Gmail accounts, the Google Cloud OAuth app must pass
Google's verification review (takes days-weeks). If everyone is on the
company's Google Workspace, set the OAuth consent screen to Internal and no
verification is needed. See README.
"""
import base64
import json

import db
import config
import crypto

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

BOUNCE_QUERY = ("from:mailer-daemon OR from:mail-daemon OR "
                'subject:(undelivered OR "delivery status notification" OR "failure notice")')


def make_flow(state=None):
    from google_auth_oauthlib.flow import Flow
    if not config.OAUTH_READY:
        raise RuntimeError(
            "Google OAuth not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET env vars (see README).")
    return Flow.from_client_config(
        {"web": {"client_id": config.GOOGLE_CLIENT_ID,
                 "client_secret": config.GOOGLE_CLIENT_SECRET,
                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                 "token_uri": "https://oauth2.googleapis.com/token",
                 "redirect_uris": [config.GMAIL_REDIRECT_URI]}},
        scopes=SCOPES, state=state, redirect_uri=config.GMAIL_REDIRECT_URI)


def _creds_from_info(info):
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=info.get("token"), refresh_token=info.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=info.get("client_id"), client_secret=info.get("client_secret"),
        scopes=SCOPES)


def get_service(account):
    """Build a Gmail API service for a gmail_accounts row (decrypts token).

    Refreshes the access token when expired and persists the refreshed token
    back, re-encrypted.
    """
    from googleapiclient.discovery import build
    info = json.loads(crypto.decrypt_token(account["token_enc"]))
    creds = _creds_from_info(info)
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        info["token"] = creds.token
        info["expiry"] = creds.expiry.isoformat() if creds.expiry else ""
        db.w("UPDATE gmail_accounts SET token_enc=? WHERE id=?",
             (crypto.encrypt_token(json.dumps(info)), account["id"]))
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_service(token_json):
    """Build a service from a *plaintext* token JSON string (tests/mocks)."""
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_creds_from_info(json.loads(token_json)),
                 cache_discovery=False)


def token_json_from_credentials(creds):
    return json.dumps({
        "token": creds.token, "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri, "client_id": creds.client_id,
        "client_secret": creds.client_secret, "scopes": list(creds.scopes or []),
        "expiry": creds.expiry.isoformat() if creds.expiry else "",
    })


def get_profile_email(service):
    return service.users().getProfile(userId="me").execute().get("emailAddress", "")


def send_message(service, to_addr, subject, body):
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to_addr
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def scan_bounces(service, max_results=25):
    """Return list of recipient addresses that appear in recent bounce notifications."""
    bounced = []
    try:
        res = service.users().messages().list(
            userId="me", q=BOUNCE_QUERY, maxResults=max_results).execute()
        for m in res.get("messages", []):
            try:
                full = service.users().messages().get(
                    userId="me", id=m["id"], format="full").execute()
                headers = {h["name"].lower(): h["value"]
                           for h in full.get("payload", {}).get("headers", [])}
                blob = json.dumps(full.get("payload", {})) + headers.get("subject", "")
                import re
                for addr in set(re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", blob)):
                    if "mailer-daemon" not in addr and "mail-daemon" not in addr:
                        bounced.append(addr.lower())
                fr = headers.get("x-failed-recipients", "")
                if fr:
                    bounced.append(fr.lower())
            except Exception:
                continue
    except Exception:
        pass
    return list(set(bounced))
