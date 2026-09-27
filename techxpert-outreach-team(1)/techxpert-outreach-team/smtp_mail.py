"""Gmail sending and bounce scanning without Google OAuth.

Each user connects their own Gmail address with a Google App Password
(myaccount.google.com > Security > App passwords; requires 2-Step
Verification). The App Password is Fernet-encrypted at rest via crypto.py
and decrypted only at the moment a connection is made. It is never logged
or rendered.

Sending: smtp.gmail.com:587 with STARTTLS (same 500/day Gmail limit as the
API path, same deliverability - it is the same mailbox sending).

Bounce scanning: imap.gmail.com:993, searching recent mail for
mailer-daemon / delivery-failure notices and extracting the failed
recipient addresses, mirroring the old Gmail API logic.
"""
import imaplib
import re
import smtplib
from email import message_from_bytes
from email.mime.text import MIMEText

import crypto

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def clean_app_password(raw):
    """Google shows App Passwords as 'abcd efgh ijkl mnop'; SMTP/IMAP need
    the 16 characters without spaces."""
    return re.sub(r"\s+", "", raw or "").strip()


def valid_app_password(raw):
    return len(clean_app_password(raw)) == 16


def _decrypt(account):
    return crypto.decrypt_token(account["password_enc"])


def send_message(account, to_addr, subject, body):
    """Send one plain-text email via Gmail SMTP. Raises on failure."""
    password = _decrypt(account)
    msg = MIMEText(body or "", "plain", "utf-8")
    msg["From"] = account["email"]
    msg["To"] = to_addr
    msg["Subject"] = subject or ""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(account["email"], password)
        s.send_message(msg)
    return True


def verify_credentials(email, app_password):
    """Check an email + App Password pair by logging into SMTP.
    Returns (ok: bool, error: str). Used at connect time so typos are
    caught immediately instead of at first send."""
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(email, clean_app_password(app_password))
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, ("Gmail rejected the login. Check the address and the "
                       "16-character App Password (no spaces).")
    except Exception as e:
        return False, f"Could not reach Gmail: {str(e)[:150]}"


def scan_bounces(account, max_results=25):
    """Return list of recipient addresses that appear in recent bounce
    notifications in the account's inbox. Failures (network, auth, IMAP
    quirks) degrade to an empty list - never raise into the caller."""
    bounced = []
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=15)
        try:
            mail.login(account["email"], _decrypt(account))
            mail.select("INBOX", readonly=True)
            # Recent mail first; look at the newest ~100 messages.
            typ, data = mail.search(None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()
            recent = ids[-100:]
            checked = 0
            for num in reversed(recent):
                if checked >= max_results * 4:
                    break
                typ, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not raw:
                    continue
                m = message_from_bytes(raw)
                frm = (m.get("From", "") or "").lower()
                subj = (m.get("Subject", "") or "").lower()
                is_bounce = (
                    "mailer-daemon" in frm or "mail-daemon" in frm
                    or "mail delivery subsystem" in frm
                    or ("undelivered" in subj and ("mail" in frm or "daemon" in frm))
                    or "delivery status notification" in subj
                    or "failure notice" in subj
                )
                if not is_bounce:
                    continue
                checked += 1
                blob = ""
                if m.is_multipart():
                    for part in m.walk():
                        ctype = part.get_content_type()
                        if ctype in ("text/plain", "message/delivery-status"):
                            try:
                                payload = part.get_payload(decode=True) or b""
                                blob += payload.decode("utf-8", errors="ignore") + "\n"
                            except Exception:
                                continue
                else:
                    try:
                        payload = m.get_payload(decode=True) or b""
                        blob = payload.decode("utf-8", errors="ignore")
                    except Exception:
                        blob = ""
                blob += subj
                for addr in set(EMAIL_RE.findall(blob)):
                    low = addr.lower()
                    if "mailer-daemon" not in low and "mail-daemon" not in low:
                        bounced.append(low)
        finally:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass
    except Exception:
        # Sandbox/offline/IMAP flakiness: degrade gracefully.
        return []
    return list(set(bounced))


def scan_replies(account, max_messages=100):
    """Return unique From addresses seen in recent non-bounce inbox mail.

    Reply detection rides on this scan: any address that emailed the sender
    account (and is not a mailer-daemon or the account itself) is treated as
    a reply candidate. The caller intersects with its own lead emails before
    marking anyone replied. Never raises - degrades to an empty list.
    """
    found = []
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=15)
        try:
            mail.login(account["email"], _decrypt(account))
            mail.select("INBOX", readonly=True)
            typ, data = mail.search(None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()
            own = (account["email"] or "").lower()
            for num in reversed(ids[-max_messages:]):
                typ, msg_data = mail.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not raw:
                    continue
                m = message_from_bytes(raw)
                frm = (m.get("From", "") or "")
                low = frm.lower()
                if "mailer-daemon" in low or "mail-daemon" in low:
                    continue
                for addr in set(EMAIL_RE.findall(frm)):
                    if addr.lower() != own:
                        found.append(addr.lower())
        finally:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass
    except Exception:
        return []
    return list(set(found))
