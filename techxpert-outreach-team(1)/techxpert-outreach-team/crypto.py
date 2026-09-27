"""Fernet encryption for Gmail OAuth tokens at rest.

Tokens are encrypted before they touch the database and decrypted only at the
moment a Gmail API call is made. They are never logged or rendered.
"""
import os

from cryptography.fernet import Fernet, InvalidToken

import config

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        key = config.FERNET_KEY
        if not key:
            # Local-dev convenience only: ephemeral key, tokens won't survive
            # a restart. Production MUST set FERNET_KEY (see README).
            print("[crypto] WARNING: FERNET_KEY not set; using ephemeral key. "
                  "Set FERNET_KEY for persistent encrypted tokens.")
            key = Fernet.generate_key().decode()
        else:
            key = key.strip()
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_token(token_json: str) -> str:
    """Encrypt a token JSON string for storage. Returns opaque ciphertext."""
    return _get_fernet().encrypt(token_json.encode("utf-8")).decode("utf-8")


def decrypt_token(token_enc: str) -> str:
    """Decrypt stored ciphertext back to the token JSON string."""
    try:
        return _get_fernet().decrypt(token_enc.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("Could not decrypt stored Gmail token: FERNET_KEY "
                           "does not match the key used at connect time.")
