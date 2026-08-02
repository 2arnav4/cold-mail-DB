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
import os
import re
import time
import random
import string
import smtplib
import socket

import dns.resolver

INTER_CALL_DELAY = 0.3

# The domain half deliberately allows any number of labels. An earlier version
# was `[\w\-]+\.[a-z]{2,}`, which permits exactly one dot and therefore rejected
# every `.co.in`, `.co.uk` and subdomain address as "invalid syntax" -- without
# issuing a single DNS or SMTP query. On India-focused outreach that is not an
# edge case, and because a syntax failure returns False rather than None those
# contacts were discarded permanently instead of held back.
EMAIL_SYNTAX_RE = re.compile(r'^[\w.+\-]+@[\w\-]+(\.[\w\-]+)*\.[a-z]{2,}$', re.I)


def verify_local(email: str) -> bool:
    if not EMAIL_SYNTAX_RE.match(email):
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

# These MUST resolve in public DNS. "coldmaildb.local" does not, so any server
# that verifies the sender domain answers `450 4.1.8 Sender address rejected:
# Domain not found` and the address comes back unverifiable for a reason that
# has nothing to do with the address.
SMTP_MAIL_FROM = os.environ.get("GMAIL_ADDRESS") or "verify@coldmaildb.local"
SMTP_HELO_DOMAIN = SMTP_MAIL_FROM.split("@", 1)[-1]

# RFC 3463 enhanced status code at the front of a reply, e.g. "5.7.1".
ENHANCED_STATUS_RE = re.compile(rb"^\s*([245])\.(\d+)\.(\d+)")

# Replies that are about US -- our IP, our sender domain, our reputation --
# rather than about the recipient's mailbox.
SENDER_SIDE_RE = re.compile(
    rb"spamhaus|spamcop|barracuda|sorbs|dnsbl|\brbl\b|black-?list|block-?list"
    rb"|blocked using|banned|reputation|not authori[sz]ed|access denied"
    rb"|client host|helo|sender address rejected|domain not found"
    rb"|service unavailable|too many|rate limit|try again|policy reasons",
    re.I,
)


def classify_rejection(message) -> bool | None:
    """Decide whether a permanent 5xx at RCPT TO means "this mailbox does not
    exist" (False) or "this server refuses to talk to you" (None).

    This distinction is the difference between correctly discarding a dead
    address and wrongly deleting a live contact because our sending IP is on a
    blocklist. Without it, every 550/551/553/554 mapped straight to False, and
    `send_emails.py` calls `move_to_wrong_address()` on False -- so a single
    blocklist listing quarantined working addresses in bulk. Verified: 550 5.7.1
    "Client host blocked using Spamhaus" was being read as a dead mailbox.

    Order:
      1. Enhanced status: 5.1.x is addressing (bad mailbox) -> False.
         5.7.x is policy/security, i.e. about the sender -> None.
      2. Failing that, keyword match on known sender-side language -> None.
      3. Otherwise assume it really is a bad mailbox -> False.
    """
    if isinstance(message, str):
        message = message.encode("utf-8", "replace")

    m = ENHANCED_STATUS_RE.match(message)
    if m:
        status_class, subject = m.group(1), m.group(2)
        if status_class != b"5":
            # 4.x.x is a TEMPORARY failure. Never permanent, whatever the
            # subject digit says -- 4.1.8 is a rejected sender domain, not a
            # dead mailbox.
            print(f"  [SMTP] temporary {status_class.decode()}.x.x, holding back: {message[:90]!r}")
            return None
        if subject == b"1":         # 5.1.x -- bad destination mailbox
            return False
        if subject == b"7":         # 5.7.x -- policy/security: about us
            print(f"  [SMTP] policy rejection, not a dead mailbox: {message[:90]!r}")
            return None

    if SENDER_SIDE_RE.search(message):
        print(f"  [SMTP] sender-side rejection, not a dead mailbox: {message[:90]!r}")
        return None

    return False


def verify_smtp(email: str) -> bool | None:
    """Direct SMTP RCPT TO probe."""
    # SMTP commands are ASCII unless both ends negotiate SMTPUTF8 (RFC 6531).
    # smtplib encodes commands with command_encoding, which defaults to ascii,
    # so `server.rcpt("kristóf@turbine.ai")` raises UnicodeEncodeError from deep
    # inside putcmd -- uncaught, that killed an entire send run on the first
    # contact. Hold these back rather than probing: unverifiable is honest here,
    # and it is emphatically not the same as invalid.
    try:
        email.encode("ascii")
    except UnicodeEncodeError:
        print(f"  [SMTP] non-ASCII address, cannot probe over plain SMTP: {email}")
        return None

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
            return classify_rejection(message)
        print(f"  [SMTP] inconclusive response {code}: {message}")
        return None
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        print(f"  [SMTP] connection to {mx_host} failed: {e}")
        return None
    except UnicodeError as e:
        # Belt and braces: a non-ASCII byte can still reach smtplib via a server
        # reply or an IDN hostname. Never let an encoding problem read as a
        # verdict about the address.
        print(f"  [SMTP] encoding error probing {email}: {e}")
        return None
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


GOOGLE_MX_SUBSTRINGS = ("google.com", "googlemail.com")


def is_google_hosted(domain: str) -> bool:
    """Gmail and Google Workspace-hosted domains accept RCPT TO broadly at
    the protocol level regardless of whether the domain owner actually
    configured a catch-all alias -- that's just how Google's MTA behaves.
    Probing them the same way as a real catch-all server would flag every
    single Google-hosted domain as "catch-all", which is the vast majority
    of startups and would hold back good addresses for the wrong reason.
    This is a known, pre-existing accepted blind spot (see module docstring),
    not something the catch-all probe can meaningfully resolve."""
    try:
        records = dns.resolver.resolve(domain, "MX")
        return any(any(s in str(r.exchange).lower() for s in GOOGLE_MX_SUBSTRINGS) for r in records)
    except Exception:
        return False


def is_catchall_domain(domain: str) -> bool | None:
    """Probes an obviously-fake address at `domain` to see whether the mail
    server accepts RCPT TO for literally anyone (a catch-all/accept-all
    config). Such domains only reject unknown mailboxes later, during actual
    delivery -- not at RCPT time -- so a 250 on the real address tells us
    nothing there. Returns True (catch-all, real-address result untrustworthy),
    False (domain properly rejects unknowns, real-address result trustworthy),
    or None (couldn't determine, e.g. MX/connection issue).

    Skips Google-hosted domains entirely -- see is_google_hosted."""
    if is_google_hosted(domain):
        return False
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
