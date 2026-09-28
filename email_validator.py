"""Free email validation: syntax -> disposable check -> MX lookup -> SMTP probe.

Free tier of validation, not NeverBounce. SMTP probing is probabilistic:
big providers (Gmail/Yahoo/Microsoft) throttle or block probes, catch-all
servers accept everything, and greylisting defers. Verdicts:

    valid    - SMTP server accepted RCPT TO for the address (non-catch-all domain)
    invalid  - bad syntax, disposable domain, no MX, or SMTP rejected the mailbox
    risky    - domain is catch-all, or probe was inconclusive but not a hard fail
    unknown  - timeout / connection blocked / greylisted (retry later)

Usage:
    from email_validator import validate_email, validate_emails
    print(validate_email("info@example.com"))
    print(validate_emails(["a@x.com", "b@y.com"], max_workers=5))
"""
import os
import re
import time
import smtplib
import secrets
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------- syntax ---

# Solid practical regex: local part, @, domain with at least one dot + TLD.
SYNTAX_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}$"
)
MAX_EMAIL_LEN = 254

# ------------------------------------------------------- disposable list ---

_DISPOSABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "disposable_domains.txt")
_disposable_cache = None


def disposable_domains():
    global _disposable_cache
    if _disposable_cache is None:
        domains = set()
        try:
            with open(_DISPOSABLE_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip().lower()
                    if line and not line.startswith("#"):
                        domains.add(line)
        except OSError:
            pass
        _disposable_cache = domains
    return _disposable_cache


def is_disposable(domain):
    domain = domain.lower()
    dd = disposable_domains()
    # match exact domain or any parent (e.g. sub.mailinator.com)
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in dd:
            return True
    return False


# ------------------------------------------------------------------ MX ---

CONNECT_TIMEOUT = 10   # per SMTP connection
EMAIL_BUDGET = 30      # max seconds spent per email overall
SMTP_PORT = 25         # override in tests to point at a local fake server


def _mx_via_doh(domain, timeout):
    """MX lookup via DNS-over-HTTPS (Google). Fallback when direct DNS is blocked."""
    try:
        import requests
        r = requests.get(
            "https://dns.google/resolve",
            params={"name": domain, "type": "MX"},
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        hosts = []
        for ans in data.get("Answer", []) or []:
            if ans.get("type") == 15:  # MX
                parts = ans.get("data", "").split()
                if len(parts) == 2:
                    pref = int(parts[0])
                    host = parts[1].rstrip(".")
                    if host:  # skip empty/garbage answers
                        hosts.append((pref, host))
        return sorted(hosts, key=lambda x: x[0])
    except Exception:
        return []


def mx_hosts(domain, timeout=CONNECT_TIMEOUT):
    """Return [(preference, host)] sorted by preference. Empty list = no MX."""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        resolver.timeout = timeout
        answers = resolver.resolve(domain, "MX")
        hosts = sorted(
            ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
            key=lambda x: x[0],
        )
        if hosts:
            return hosts
    except Exception:
        pass
    # Fallback: DNS-over-HTTPS (works where direct port-53 is blocked)
    return _mx_via_doh(domain, timeout)


# ---------------------------------------------------------------- SMTP ---

def _smtp_probe(mx_host, mail_from, rcpt_to, timeout, deadline):
    """Single RCPT TO probe against one MX host.

    Returns (stage, code, message) where stage is "mailfrom" or "rcpt".
    Raises on connection/timeout errors.
    """
    if time.time() > deadline:
        raise TimeoutError("email budget exceeded")
    smtp = smtplib.SMTP(timeout=timeout)
    try:
        smtp.connect(mx_host, SMTP_PORT)
        smtp.ehlo_or_helo_if_needed()
        code, _ = smtp.mail(mail_from)
        if code >= 500:
            return "mailfrom", code, "MAIL FROM rejected"
        code, msg = smtp.rcpt(rcpt_to)
        try:
            smtp.quit()
        except Exception:
            pass
        return "rcpt", code, msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
    finally:
        try:
            smtp.close()
        except Exception:
            pass


def _is_greylist(code, msg):
    if code in (421, 450, 451):
        m = (msg or "").lower()
        return any(k in m for k in ("greylist", "greylisted", "try again",
                                   "temporarily deferred", "throttl", "rate limit"))
    return False


def validate_email(email, mail_from=None, timeout=CONNECT_TIMEOUT,
                   budget=EMAIL_BUDGET):
    """Validate one email address. Returns a dict with verdict + detail."""
    started = time.time()
    deadline = started + budget
    email = (email or "").strip()
    result = {"email": email, "verdict": "unknown", "reason": "",
              "mx_host": "", "catch_all": False,
              "duration": 0.0}

    def finish(verdict, reason):
        result["verdict"] = verdict
        result["reason"] = reason
        result["duration"] = round(time.time() - started, 2)
        return result

    # 1. syntax
    if not email or len(email) > MAX_EMAIL_LEN or not SYNTAX_RE.match(email):
        return finish("invalid", "bad syntax")
    local, _, domain = email.rpartition("@")
    domain = domain.lower()

    # 2. disposable
    if is_disposable(domain):
        return finish("invalid", "disposable domain")

    # 3. MX lookup
    hosts = mx_hosts(domain, timeout=min(timeout, max(1, deadline - time.time())))
    if not hosts:
        return finish("invalid", "no MX record")

    # 4. SMTP probe
    if mail_from is None:
        host = socket.getfqdn() or "localhost"
        mail_from = f"verify@{host}"
    probe_local = f"catchtest-{secrets.token_hex(6)}"

    last_error = ""
    got_response = False  # True once any MX host answers SMTP (any code)
    for _, mx_host in hosts:
        if time.time() > deadline:
            break
        try:
            # 4a. catch-all detection: probe a random nonexistent address first
            stage, code, msg = _smtp_probe(mx_host, mail_from, f"{probe_local}@{domain}",
                                          timeout, deadline)
            got_response = True
            if stage == "mailfrom":
                last_error = f"MAIL FROM rejected ({code})"
                break  # our probe identity is rejected; other hosts will do the same
            if 200 <= code < 300:
                result["catch_all"] = True
                result["mx_host"] = mx_host
                return finish("risky", "catch-all server: accepts any address")
            if _is_greylist(code, msg):
                last_error = f"greylisted ({code})"
                continue  # try next MX host

            # 4b. real probe
            stage, code, msg = _smtp_probe(mx_host, mail_from, email, timeout, deadline)
            got_response = True
            result["mx_host"] = mx_host
            if stage == "mailfrom":
                last_error = f"MAIL FROM rejected ({code})"
                break
            if 200 <= code < 300:
                return finish("valid", f"mailbox accepted by {mx_host}")
            if 500 <= code < 600:
                return finish("invalid", f"mailbox rejected ({code})")
            if _is_greylist(code, msg):
                last_error = f"greylisted ({code})"
                continue
            # other 4xx: soft fail, try next host
            last_error = f"deferred ({code})"
        except (smtplib.SMTPException, OSError, TimeoutError) as e:
            last_error = f"{type(e).__name__}: {str(e)[:80]}"
            continue

    # exhausted hosts / budget
    if not got_response:
        # No MX host ever answered SMTP (e.g. outbound port 25 is blocked on
        # this server, as on Vercel). That is evidence about OUR network, not
        # the mailbox: MX exists, so the address stays queueable-but-unverified
        # ("risky"), exactly like a catch-all server.
        return finish("risky",
                      "mailbox unverifiable from this server: no SMTP response "
                      "from any MX host (outbound SMTP may be blocked here); MX exists")
    if "greylist" in last_error.lower() or "deferred" in last_error.lower():
        return finish("unknown", f"probe deferred: {last_error} (retry later)")
    if last_error:
        return finish("unknown", f"probe blocked/failed: {last_error}")
    return finish("unknown", "no MX host reachable")


def validate_emails(emails, max_workers=5, mail_from=None, progress_cb=None):
    """Validate a list of emails with polite concurrency (default 5 workers)."""
    emails = [e for e in emails if e]
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(validate_email, e, mail_from): e for e in emails}
        done = 0
        for fut in as_completed(future_map):
            done += 1
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"email": future_map[fut], "verdict": "unknown",
                                "reason": f"validator error: {e}", "mx_host": "",
                                "catch_all": False, "duration": 0.0})
            if progress_cb:
                progress_cb(done, len(emails))
    # keep input order
    order = {e: i for i, e in enumerate(emails)}
    results.sort(key=lambda r: order.get(r["email"], 0))
    return results
