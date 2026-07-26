"""
email_verify.py — free-only email verification: local MX check -> SMTP RCPT
probe -> catch-all check.

(Abstract API and MillionVerifier were removed — both permanently out of
credits with no realistic path to more. NeverBounce, ZeroBounce,
QuickEmailVerification, and Bouncer were tried earlier and dropped for the
same reason or because they hard-require a business email domain.)

The SMTP probe connects directly to the recipient's mail server and issues a
RCPT TO without sending anything. It's free and unlimited, but has a real
blind spot: catch-all domains accept RCPT TO for virtually any address and
only reject an unknown mailbox later, during actual delivery. `is_catchall_domain`
detects this by probing an obviously-fake address at the same domain — if
that also gets accepted, the real address's "valid" result can't be trusted.

Risk posture: when nothing can produce a confident answer (SMTP blocked/
inconclusive, or the domain turns out to be catch-all), verify_email returns
None rather than guessing. Callers should treat None as "hold back, don't
send, don't discard either" — not the same as a confirmed-bad address, but
not safe to send to either.

Reusable by send_emails.py and scraper.py.
"""
import re
import time
import random
import string
import smtplib
import socket

import dns.resolver

INTER_CALL_DELAY = 0.3


def verify_local(email: str) -> bool:
    if not re.match(r'^[\w\.\+\-]+@[\w\-]+\.[a-z]{2,}$', email, re.I):
        print(f"  [Validation] Invalid syntax: {email}")
        return False

    domain = email.split('@')[1]
    try:
        records = dns.resolver.resolve(domain, 'MX')
        if records:
            return True
    except dns.resolver.NoAnswer:
        print(f"  [Validation] No mail server found for domain: {domain}")
        return False
    except dns.resolver.NXDOMAIN:
        print(f"  [Validation] Domain does not exist: {domain}")
        return False
    except Exception as e:
        print(f"  [Validation] DNS error for {domain}: {e}")
        return True  # Fallback to True if DNS fails to avoid false positives
    return False


SMTP_TIMEOUT = 6
SMTP_HELO_DOMAIN = "coldmaildb.local"
SMTP_MAIL_FROM = "verify@coldmaildb.local"


def verify_smtp(email: str) -> bool | None:
    """Direct SMTP RCPT TO probe."""
    domain = email.split('@')[1]
    try:
        records = sorted(dns.resolver.resolve(domain, 'MX'), key=lambda r: r.preference)
        mx_host = str(records[0].exchange).rstrip('.')
    except Exception as e:
        print(f"  [SMTP] MX lookup failed for {domain}: {e}")
        return None

    server = None
    try:
        server = smtplib.SMTP(timeout=SMTP_TIMEOUT)
        server.connect(mx_host, 25)
        server.helo(SMTP_HELO_DOMAIN)
        server.mail(SMTP_MAIL_FROM)
        code, message = server.rcpt(email)

        if code == 250:
            return True
        if code in (550, 551, 553, 554):
            return False
        print(f"  [SMTP] inconclusive response {code}: {message}")
        return None
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        print(f"  [SMTP] connection to {mx_host} failed: {e}")
        return None
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


def is_catchall_domain(domain: str) -> bool | None:
    """Probes an obviously-fake address at `domain` to see whether the mail
    server accepts RCPT TO for literally anyone (a catch-all/accept-all
    config). Such domains only reject unknown mailboxes later, during actual
    delivery -- not at RCPT time -- so a 250 on the real address tells us
    nothing there. Returns True (catch-all, real-address result untrustworthy),
    False (domain properly rejects unknowns, real-address result trustworthy),
    or None (couldn't determine, e.g. MX/connection issue)."""
    fake_local = "zzz-nonexistent-" + "".join(random.choices(string.digits, k=10))
    return verify_smtp(f"{fake_local}@{domain}")


def verify_email(email: str):
    """Returns (is_valid, checked_by).

    is_valid is one of:
      True  -> confirmed deliverable (SMTP accepted it, and the domain is
               not a catch-all)
      False -> confirmed bad (bad syntax, dead domain, or explicit SMTP reject)
      None  -> unverifiable: either the domain is catch-all, or the SMTP
               probe couldn't get a real answer at all (blocked, timed out,
               target down) and there is nothing else left to try. Callers
               should hold back rather than send -- this is not a confirmed
               address, but it's also not confirmed bad, so don't discard it
               either.
    """
    if not verify_local(email):
        return False, "local_mx"

    domain = email.split("@", 1)[1]

    result = verify_smtp(email)
    time.sleep(INTER_CALL_DELAY)

    if result is False:
        return False, "smtp"

    if result is True:
        catchall = is_catchall_domain(domain)
        time.sleep(INTER_CALL_DELAY)
        if catchall:
            return None, "catchall_unverifiable"
        return True, "smtp"

    # SMTP was inconclusive (blocked/timed out/no real answer) and there's no
    # paid fallback left to consult -- hold back rather than assume it's fine.
    return None, "smtp_inconclusive"
