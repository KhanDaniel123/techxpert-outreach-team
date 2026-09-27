"""Google Sign-In (login) flow. Separate from the Gmail-connect flow in
gmail_oauth.py: login uses openid/email/profile scopes only; connecting a
Gmail account for sending uses the restricted gmail.send + gmail.readonly
scopes and stores an encrypted token per account.
"""
import json

import requests

import config

LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def make_login_flow(state=None):
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
                 "redirect_uris": [config.LOGIN_REDIRECT_URI]}},
        scopes=LOGIN_SCOPES, state=state,
        redirect_uri=config.LOGIN_REDIRECT_URI)


def fetch_userinfo(credentials):
    """Return (google_sub, email, name) for the authenticated Google user."""
    r = requests.get("https://www.googleapis.com/oauth2/v3/userinfo",
                     headers={"Authorization": f"Bearer {credentials.token}"},
                     timeout=20)
    r.raise_for_status()
    info = r.json()
    sub = info.get("sub", "")
    email = info.get("email", "")
    name = info.get("name", "") or email.split("@")[0]
    if not sub or not email:
        raise RuntimeError("Google did not return a user identity.")
    return sub, email, name
