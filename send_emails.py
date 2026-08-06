#!/usr/bin/env python3
"""
Cold Mail Sender
----------------
Reads contacts from turso-full.db, sends cold emails via Gmail SMTP up to
CONFIG["daily_limit"] per day, tracks sent emails in sent_log.json, and
attaches a resume.

Setup:
  1. Fill in CONFIG below (your Gmail, app password, resume path)
  2. Edit template.txt with your email body
  3. Run:  python3 send_emails.py
  4. To do a dry run (no emails sent): python3 send_emails.py --dry-run

Gmail App Password (NOT your regular password):
  https://myaccount.google.com/apppasswords
  (Requires 2FA to be enabled on your Google account)
"""

import sqlite3
import smtplib
import json
import os
import sys
import time
import argparse
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

# ─────────────────────────────────────────────
#  CONFIG — loaded from .env file automatically
# ─────────────────────────────────────────────
import os as _os


def _load_env(path=".env"):
    """Parse a .env file and inject into os.environ (no dependencies needed)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                _os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass  # .env is optional; fall back to real env vars


_load_env()

from email_verify import verify_email  # noqa: E402 (must load after _load_env populates os.environ)

CONFIG = {
    "your_email": _os.environ.get("GMAIL_ADDRESS", ""),
    "app_password": _os.environ.get("GMAIL_APP_PASSWORD", ""),
    "your_name": "Arnav Singla",
    "resume_path": "ARNAV-RESUME.pdf",
    "db_path": "turso-full.db",
    "template_path": "template.txt",
    "log_path": "sent_log.json",
    "daily_limit": 100,
    "tracker_url": _os.environ.get("TRACKER_URL", ""),
    "tracker_secret": _os.environ.get("TRACKER_SECRET", ""),
    # Lowest email_confidence worth mailing. score_confidence.py writes 15 for
    # addresses a pattern generator invented (firstname@domain) and 75+ for
    # ones that were actually found somewhere. Mailing the 15s is what produced
    # the 811 entries in sent_log_failed.json, so they stay out of the queue
    # until find_real_emails.py replaces them with a published address.
    "min_confidence": 75,
    # Attach the resume PDF to a first cold email, or link it in the body.
    # An attachment from an unknown sender raises the spam score, and the
    # generated batch emails already carry arnav24.tech/resume.pdf as a link,
    # so attaching as well sends the same document twice. Decks in
    # OUTREACH_DECKS are unaffected: those are the tier-one sends where the
    # attachment is the point.
    "attach_resume": False,
    # Restrict a run to one evidence tier. "" means every tier at or above
    # min_confidence. Set to "published" for a low-bounce batch: those addresses
    # were read off the company's own website, where the list tier is bought
    # data that is now several months stale.
    "send_provenance": "",
}


def utc_stamp() -> str:
    """Current UTC as 'YYYY-MM-DD HH:MM:SS'.

    datetime.utcnow() is deprecated from Python 3.12 and returns a naive
    datetime that merely happens to hold UTC, which is how timezone bugs start.
    The stored format is unchanged, so existing log entries stay comparable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def tracker_headers(cfg: dict, content_type: str = "") -> dict:
    """Auth header for the tracker's private endpoints. Must match the
    TRACKER_SECRET set on the Render service, or those routes return 401."""
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    secret = cfg.get("tracker_secret", "")
    if secret:
        headers["X-Tracker-Key"] = secret
    return headers

# ─────────────────────────────────────────────────────────────────────────────
#  PERSONALIZED EMAILS
#  Key   = recipient email address (exact match from the PDF)
#  Value = { "subject": "...", "body": "..." }
#  For any contact NOT in this dict, the generic template.txt is used.
# ─────────────────────────────────────────────────────────────────────────────
# Recipient copy lives in personalized_emails.local.json, which is gitignored.
# This repository is public, and the dict held named individuals' work
# addresses as keys. A local file keeps the same behaviour without publishing
# who is being contacted -- the same reason outreach/.gitignore excludes out/.
PERSONALIZED_EMAILS = {}


def _load_personalized(path="personalized_emails.local.json"):
    """Merge the local recipient copy in, if present. Absent file is fine:
    contacts without an entry fall through to the sector template."""
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(full):
        return 0
    try:
        with open(full, encoding="utf-8") as f:
            PERSONALIZED_EMAILS.update(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARNING: {path} unreadable, ignoring ({exc})")
        return 0
    return len(PERSONALIZED_EMAILS)


_load_personalized()

# ─────────────────────────────────────────────────────────────────────────────
#  TIER ONE: outreach/ decks
#
#  outreach/ builds a per-company email and a five slide technical deck, then
#  writes outreach/out/<slug>/send.json. Those are merged into
#  PERSONALIZED_EMAILS below so they ride this sender, its daily cap, the
#  tracker and the bounce handling rather than a second delivery path.
#
#  The deck path travels with them in OUTREACH_DECKS, and build_email attaches
#  it alongside the resume. A contact with a deck gets two attachments; every
#  other contact is untouched.
#
#  Hand written entries in PERSONALIZED_EMAILS win on conflict, because those
#  were typed deliberately and a stale build should never silently replace one.
# ─────────────────────────────────────────────────────────────────────────────
OUTREACH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outreach", "out")
OUTREACH_DECKS = {}


def load_outreach_sends(directory: str = OUTREACH_DIR) -> tuple:
    """Read outreach/out/*/send.json. Returns ({email: {...}}, {email: deck_path})."""
    emails, decks = {}, {}
    if not os.path.isdir(directory):
        return emails, decks

    for slug in sorted(os.listdir(directory)):
        path = os.path.join(directory, slug, "send.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  WARNING: outreach/out/{slug}/send.json unreadable, skipping ({exc})")
            continue

        addr = (payload.get("recipient_email") or "").strip().lower()
        if not addr or not payload.get("subject") or not payload.get("body"):
            print(f"  WARNING: outreach/out/{slug}/send.json is incomplete, skipping")
            continue

        emails[addr] = {"subject": payload["subject"], "body": payload["body"]}

        deck = payload.get("deck")
        # A missing deck is not fatal. The email is still better than the
        # generic template, and refusing to send would be the wrong tradeoff.
        if deck and os.path.exists(deck):
            decks[addr] = deck
        elif deck:
            print(f"  WARNING: deck missing for {addr} at {deck}, sending without it")

    return emails, decks


def install_outreach_sends():
    """Merge tier one sends in without clobbering hand written entries."""
    emails, decks = load_outreach_sends()
    added = 0
    for addr, payload in emails.items():
        if addr in PERSONALIZED_EMAILS:
            print(f"  NOTE: {addr} is hand written in PERSONALIZED_EMAILS, keeping that over the outreach build")
            continue
        PERSONALIZED_EMAILS[addr] = payload
        OUTREACH_DECKS[addr] = decks.get(addr)
        added += 1
    if added:
        with_deck = sum(1 for a in OUTREACH_DECKS.values() if a)
        print(f"  Loaded {added} outreach email(s), {with_deck} with a deck attached")
    return added


# ─────────────────────────────────────────────
#  Contact filter — adjust to target who you want
#
#  Two gates, doing different jobs.
#
#  `min_confidence` is about evidence: below it, nobody ever saw the address --
#  it was generated as firstname@domain. Mailing those is what filled
#  sent_log_failed.json with 811 entries.
#
#  The ROW_NUMBER filter is about restraint: a company can hold several real
#  people, and mailing two of them at a five-person startup reads as a
#  mailmerge and burns the company on the first attempt. One per company, the
#  best-evidenced one, and the runner-up stays in the database untouched for a
#  later cycle rather than being deleted.
# ─────────────────────────────────────────────
CONTACT_QUERY = """
    WITH ranked AS (
        SELECT
            ct.id        AS contact_id,
            ct.name      AS contact_name,
            ct.role      AS contact_role,
            ct.email     AS contact_email,
            ct.email_confidence AS email_confidence,
            ct.email_provenance AS email_provenance,
            ct.priority  AS priority,
            co.id        AS company_id,
            co.name      AS company_name,
            co.domain    AS company_domain,
            co.industry  AS company_industry,
            co.sector    AS company_sector,
            co.funding_stage AS funding_stage,
            ROW_NUMBER() OVER (
                PARTITION BY ct.company_id
                ORDER BY ct.email_confidence DESC,
                         ct.priority DESC,
                         ct.id ASC
            ) AS rn
        FROM contacts ct
        JOIN companies co ON ct.company_id = co.id
        WHERE
            ct.email IS NOT NULL
            AND ct.is_invalid = 0
            AND ct.email != ''
            AND COALESCE(ct.email_confidence, 0) >= :min_confidence
            AND (:provenance = '' OR ct.email_provenance = :provenance)
    )
    SELECT * FROM ranked
    WHERE rn = 1
    ORDER BY email_confidence DESC, priority DESC, contact_id ASC
"""

# ─────────────────────────────────────────────────────────────────────────────


def load_log(log_path: str) -> dict:
    """Load the sent log.
    Structure: {
      'sent': [...emails...],
      'daily': {'YYYY-MM-DD': count},
      'details': {'email': {'company': ..., 'sent_at': ...}}
    }"""
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            data = json.load(f)
            # Ensure 'details' key exists for older logs
            if "details" not in data:
                data["details"] = {}
            return data
    return {"sent": [], "daily": {}, "details": {}}


def save_log(log_path: str, log: dict):
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)


def load_failed_log(log_path: str) -> list:
    """Load the failed sends log — a list of {email, company, error, timestamp} dicts."""
    failed_path = log_path.replace(".json", "_failed.json")
    if os.path.exists(failed_path):
        with open(failed_path, "r") as f:
            return json.load(f)
    return []


def record_failed(log_path: str, contact: dict, error: str, kind: str = "send_error"):
    """Append a failed send to failed_log.json.

    `kind` is what downstream classification reads:
      "skip"       -- pre-send verification held it back; nothing was attempted
      "send_error" -- a real attempt that failed at SMTP time
      "bounce"     -- a mailer-daemon DSN (written by check_and_sync_bounces)

    This used to be inferred by testing whether `error` started with the literal
    "Failed verification:". Any rewording of that message silently reclassified
    every skip as a bounce, which is not a distinction to leave resting on a
    prefix match."""
    failed_path = log_path.replace(".json", "_failed.json")
    failed = load_failed_log(log_path)
    failed.append(
        {
            "email": contact["contact_email"],
            "name": contact.get("contact_name") or "",
            "company": contact.get("company_name") or "",
            "error": str(error),
            "kind": kind,
            "time": utc_stamp(),
        }
    )
    with open(failed_path, "w") as f:
        json.dump(failed, f, indent=2)


def sent_index(log: dict) -> set:
    """Lowercased set of everything already sent.

    Two reasons this isn't a plain `email in log["sent"]`. Local-parts are
    case-sensitive per RFC 5321, but no provider in practice treats them that
    way, so an address that differs only in case is the same mailbox and must
    not be mailed twice. And membership against a list is O(n) per contact,
    which over the whole queue is O(n*m) for no reason."""
    return {e.lower() for e in log.get("sent", [])}


def already_sent(sent_set: set, email: str) -> bool:
    return email.lower() in sent_set


def sent_today(log: dict) -> int:
    today = str(date.today())
    return log["daily"].get(today, 0)


def record_sent(log: dict, email: str, company: str = ""):
    """Record a successful send — email list, daily count, and per-email details."""
    today = str(date.today())
    log["sent"].append(email)
    log["daily"][today] = log["daily"].get(today, 0) + 1
    log["details"][email] = {
        "company": company,
        "sent_at": utc_stamp(),
    }


def sync_tracker_from_logs(cfg: dict):
    """Re-sync all local sent/bounce/open logs to the Render tracker.
    Also downloads new opens from the tracker to back them up locally,
    making the dashboard fully persistent across Render restarts."""
    tracker_url = cfg.get("tracker_url", "").rstrip("/")
    if not tracker_url:
        return

    import urllib.request, json as _json

    log = load_log(cfg["log_path"])
    failed = load_failed_log(cfg["log_path"])

    # Load local opens backup
    opens_path = cfg["log_path"].replace(".json", "_opens.json")
    local_opens = []
    if os.path.exists(opens_path):
        try:
            with open(opens_path, "r") as f:
                local_opens = _json.load(f)
        except Exception:
            pass

    # Exclude any false opens for bounced emails from the local backup
    bounced_emails_set = {b["email"].lower() for b in failed}
    local_opens = [
        o for o in local_opens if o["email"].lower() not in bounced_emails_set
    ]

    # 1. Download current opens from Render to back them up locally
    try:
        req = urllib.request.Request(
            f"{tracker_url}/api/stats", headers=tracker_headers(cfg), method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            server_opens = _json.loads(resp.read())

        # Merge server opens into local backup (deduplicate by (email, opened_at)), skipping bounces
        local_keys = {(o["email"], o["opened_at"]) for o in local_opens}
        added_new = False
        for o in server_opens:
            if o["email"].lower() in bounced_emails_set:
                continue  # Skip false opens on bounced emails
            key = (o["email"], o["opened_at"])
            if key not in local_keys:
                local_opens.append(o)
                local_keys.add(key)
                added_new = True

        # Always write the clean opens list to the local backup
        local_opens.sort(key=lambda x: x.get("opened_at", ""), reverse=True)
        with open(opens_path, "w") as f:
            _json.dump(local_opens, f, indent=2)
    except Exception as err:
        print(f"  Warning: Could not fetch opens backup from server: {err}")

    # Build sends payload from the details dict (has company + sent_at)
    sends = [
        {
            "email": email,
            "company": info.get("company", ""),
            "sent_at": info.get("sent_at", ""),
        }
        for email, info in log.get("details", {}).items()
    ]
    # Fall back: emails in sent[] with no details entry get a bare record
    details_emails = set(log.get("details", {}).keys())
    for email in log.get("sent", []):
        if email not in details_emails:
            sends.append({"email": email, "company": "", "sent_at": ""})

    # Entries caught by the pre-send check were never actually sent -- those are
    # skips, not bounces. Everything else was a real attempt that failed, either
    # a genuine mailer-daemon bounce (which carries its own bounce_type) or an
    # SMTP-time exception (which doesn't -- default that to "unknown" rather
    # than assuming hard).
    #
    # Classification reads the explicit "kind" field. Records written before
    # that field existed fall back to the old prefix test so history still
    # classifies the same way.
    bounces = []
    skips = []
    for b in failed:
        reason = b.get("error", "Bounce — invalid address")
        kind = b.get("kind")
        if kind is None:
            kind = (
                "skip"
                if isinstance(reason, str) and reason.startswith("Failed verification:")
                else "bounce"
            )
        if kind == "skip":
            skips.append({
                "email": b["email"],
                "company": b.get("company", ""),
                "reason": reason,
                "sent_at": b.get("time", ""),
            })
        else:
            bounces.append({
                "email": b["email"],
                "company": b.get("company", ""),
                "reason": reason,
                "bounce_type": b.get("bounce_type", "unknown"),
                "sent_at": b.get("time", ""),
            })

    try:
        payload = _json.dumps(
            {"sends": sends, "bounces": bounces, "skips": skips, "opens": local_opens}
        ).encode()
        req = urllib.request.Request(
            f"{tracker_url}/api/bulk_sync",
            data=payload,
            headers=tracker_headers(cfg, "application/json"),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        print(
            f"  Tracker synced — {result.get('synced_sends', 0)} sends, {result.get('synced_bounces', 0)} bounces, "
            f"{result.get('synced_skips', 0)} skipped, {result.get('synced_opens', 0)} opens"
        )
    except Exception as e:
        print(f"  Tracker sync warning: {e}")


def load_template(template_path: str) -> tuple:
    """
    Returns (subject, body). Template format:
      First line: Subject: <subject text>
      Blank line
      Rest: body
    """
    with open(template_path, "r") as f:
        content = f.read()

    lines = content.splitlines()
    subject = ""
    body_start = 0

    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line[len("subject:") :].strip()
            body_start = i + 1
            break

    # Skip blank lines after subject
    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    body = "\n".join(lines[body_start:])
    return subject, body


# ─────────────────────────────────────────────────────────────────────────────
#  Sector templates
#
#  companies.sector is set by classify_companies.py. Each sector gets a template
#  in templates/<sector>.txt that references the projects actually relevant to
#  it -- Lucent for fintech, the Go verifier for devtools, Student Helper for
#  consumer. Anything unclassified falls back to the generic template.txt, which
#  is why an empty templates/ directory is not an error.
#
#  There is deliberately no ai-ml sector. Claiming ML experience Arnav does not
#  have would read worse than a generic email.
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def load_sector_templates(directory: str = SECTOR_TEMPLATE_DIR) -> dict:
    """Load templates/<sector>.txt into {sector: (subject, body)}."""
    templates = {}
    if not os.path.isdir(directory):
        return templates
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".txt"):
            continue
        sector = filename[: -len(".txt")]
        subject, body = load_template(os.path.join(directory, filename))
        if subject and body:
            templates[sector] = (subject, body)
        else:
            print(f"  WARNING: templates/{filename} has no Subject: line, skipping")
    return templates


def pick_template(contact: dict, sector_templates: dict, fallback: tuple) -> tuple:
    """Returns (subject, body, label). Label is for logging only."""
    sector = (contact.get("company_sector") or "").strip().lower()
    if sector and sector in sector_templates:
        subject, body = sector_templates[sector]
        return subject, body, f"[{sector.upper()}]"
    return fallback[0], fallback[1], "[GENERIC]"


def render(template: str, contact: dict) -> str:
    """Replace {{placeholders}} with contact data."""
    result = template
    replacements = {
        "{{contact_name}}": contact.get("contact_name") or "there",
        "{{first_name}}": (contact.get("contact_name") or "there").split()[0],
        "{{company}}": contact.get("company_name") or "",
        "{{company_domain}}": contact.get("company_domain") or "",
        "{{role}}": contact.get("contact_role") or "",
        "{{industry}}": contact.get("company_industry") or "",
        "{{funding_stage}}": contact.get("funding_stage") or "",
    }
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


import re


def text_to_html(text: str, pixel_tag: str = "") -> str:
    """Convert plain text email body to clean HTML with clickable links."""
    import html as html_lib

    # Escape HTML special chars first
    escaped = html_lib.escape(text)

    # 1. Parse markdown links [Link Text](URL)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', escaped
    )

    # 2. Auto-link remaining raw https:// and http:// URLs (ignoring already linked ones)
    escaped = re.sub(
        r'(?<!href=")(?<!">)(https?://[^\s<>"]+)', r'<a href="\1">\1</a>', escaped
    )

    # Convert newlines to <br> and wrap in clean HTML
    body_html = escaped.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #222; max-width: 600px;">
{body_html}
{pixel_tag}
</body>
</html>"""


def sign_payload(encoded: str, secret: str) -> str:
    """Truncated HMAC over the encoded click payload.

    The payload carries the redirect target, and /c/ is unauthenticated by
    necessity. Without a signature anyone could base64 their own destination
    and turn the tracker domain into an open redirect pointing at whatever they
    liked -- a phishing primitive wearing your hostname. The tracker recomputes
    this and refuses to redirect if it doesn't match."""
    import hmac
    import hashlib

    digest = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return digest[:16]


def wrap_links(text, email, company, tracker_url, secret=""):
    """DO NOT CALL. Kept only so the tracker's /c/ route has a documented origin.

    Click tracking is off by design. Wiring this into build_email mangles the
    template two ways:

      1. url_pattern below treats ")" as a valid URL character, so in
         "[Pulse](https://pulse.app)" it swallows the closing paren. The
         markdown-link regex in text_to_html then fails to match and the line
         renders as literal "[Pulse](https://...)" text.
      2. For a bare URL, text_to_html auto-links whatever string is there --
         so the recipient sees the opaque tracker URL as the visible link text
         instead of "arnav24.tech".

    The links in template.txt are the point of the email. They ship untouched.
    """
    if not tracker_url or not secret:
        # Without a secret the link cannot be signed, and an unsigned redirect
        # is worse than no click tracking. Leave the real URLs in place.
        return text
    import re
    import base64

    tracker_clean = tracker_url.rstrip("/")

    # "|" is the field separator in the encoded payload and /c/ splits with
    # maxsplit=2, so a company name containing one would shift the target URL
    # into the company field and break the redirect.
    company = (company or "").replace("|", "/")

    def replace_url(match):
        url = match.group(1)
        if tracker_clean in url:
            return url
        payload = f"{email}|{company}|{url}"
        encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{tracker_clean}/c/{encoded}.{sign_payload(encoded, secret)}"

    url_pattern = r"(https?://[a-zA-Z0-9.\-_~!$&\'()*+,;=:@/%?#]+)"
    return re.sub(url_pattern, replace_url, text)


def attach_file(msg: MIMEMultipart, path: str):
    """Base64 a file onto the outer mixed container as a named attachment."""
    with open(path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
    msg.attach(part)


def build_email(cfg: dict, contact: dict, subject: str, body: str) -> MIMEMultipart:
    # Use personalized email if we have one for this exact address
    personalized = PERSONALIZED_EMAILS.get(contact["contact_email"])
    if personalized:
        final_subject = personalized["subject"]
        final_body = personalized["body"]
    else:
        final_subject = render(subject, contact)
        final_body = render(body, contact)

    # Build tracking pixel tag if TRACKER_URL is configured
    tracker_url = cfg.get("tracker_url", "").rstrip("/")
    pixel_tag = ""
    if tracker_url:
        import base64 as _b64

        payload = f"{contact['contact_email']}|{contact.get('company_name', '')}"
        encoded = _b64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        pixel_tag = f'<img src="{tracker_url}/t/{encoded}.gif" width="1" height="1" style="display:none;" />'

    # NOTE: outbound links are deliberately NOT rewritten through the tracker.
    # wrap_links stays unused on purpose -- see the warning on that function.
    # The body ships with the exact URLs written in template.txt.
    html_body = final_body

    # Send as multipart/alternative (plain text + HTML) so links are clickable
    msg = MIMEMultipart("mixed")  # outer container (holds alternative + attachment)
    msg["From"] = f"{cfg['your_name']} <{cfg['your_email']}>"
    msg["To"] = contact["contact_email"]
    msg["Subject"] = final_subject

    # Inner multipart/alternative for plain + HTML
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(final_body, "plain", "utf-8"))
    alt.attach(MIMEText(text_to_html(html_body, pixel_tag), "html", "utf-8"))
    msg.attach(alt)

    # Attach the resume only when asked for. A contact carrying a deck always
    # gets it, because a deck without the resume beside it reads as incomplete.
    resume_path = cfg["resume_path"]
    has_deck = bool(OUTREACH_DECKS.get(contact["contact_email"].strip().lower()))
    if cfg.get("attach_resume", True) or has_deck:
        if os.path.exists(resume_path):
            attach_file(msg, resume_path)
        else:
            print(
                f"  WARNING: Resume not found at '{resume_path}' — sending without attachment"
            )

    # Tier one contacts also carry a per-company deck. The resume goes first so
    # the familiar attachment is the one the reader sees at the top.
    deck_path = OUTREACH_DECKS.get(contact["contact_email"].strip().lower())
    if deck_path and os.path.exists(deck_path):
        attach_file(msg, deck_path)

    return msg


def contacted_in_db(db_path: str) -> set:
    """Addresses the send_queue already marked SENT/DONE, lowercased.

    This used to be `remove_contacted_from_db`, which DELETEd the matching rows
    from `contacts` outright. Two things were wrong with that. It matched on
    email rather than id, so one address shared by two company_ids took both
    rows down with it. And it was an unrecoverable delete of the only structured
    record of a contact -- leaving sent_log.json, an untracked file, as the sole
    history. Excluding a contact from today's queue does not require destroying
    it, so this returns a set to filter with and writes nothing."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT email FROM contacts WHERE id IN "
                "(SELECT contact_id FROM send_queue WHERE status IN ('SENT', 'DONE'))"
            ).fetchall()
        finally:
            conn.close()
        return {r[0].lower() for r in rows if r[0]}
    except Exception as e:
        print(f"  Warning: could not read send_queue history: {e}")
        return set()


def load_contacts_from_csv(csv_path: str) -> list:
    """Read contacts from a local CSV instead of turso-full.db.

    Column names are matched case-insensitively so the files in hr-database/
    work as-is despite having different headers -- `priority-outreach` leads
    with Priority/Tier, `HR_LIST 300` leads with SNo. Only Email is required.

    Rows carry contact_id None, which the send loop reads as "not backed by the
    database": a bad address is logged but no DELETE or UPDATE is attempted
    against `contacts`, because there is no row there to touch."""
    import csv as _csv

    def pick(row: dict, *names):
        for want in names:
            for key, val in row.items():
                if key and key.strip().lower() == want:
                    return (val or "").strip()
        return ""

    contacts = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            email = pick(row, "email", "contact_email")
            if not email or "@" not in email:
                continue
            contacts.append({
                "contact_id": None,
                "contact_name": pick(row, "name", "contact_name"),
                "contact_role": pick(row, "title", "role", "contact_role"),
                "contact_email": email,
                "company_id": None,
                "company_name": pick(row, "company", "company_name"),
                "company_domain": pick(row, "domain", "company_domain")
                                  or email.split("@", 1)[1],
                "company_industry": "",
                "funding_stage": "",
            })
    return contacts


def fetch_contacts(db_path: str, min_confidence: int = None,
                   provenance: str = None) -> list:
    """Contacts eligible to be mailed, best-evidenced address first, one per company.

    `min_confidence` gates on evidence, not on deliverability: an address below
    it was never found anywhere, only generated, so there is nothing for the
    SMTP probe downstream to be right about.

    `provenance` narrows further to a single tier. Passing 'published' restricts
    the run to addresses read off a company's own website, which is the lowest
    bounce risk available -- worth using for a small batch where a bad send is
    expensive."""
    if min_confidence is None:
        min_confidence = CONFIG.get("min_confidence", 0)
    if provenance is None:
        provenance = CONFIG.get("send_provenance", "")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(CONTACT_QUERY, {"min_confidence": min_confidence,
                                "provenance": provenance or ""})
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def print_summary(sent: list, skipped: int, remaining_today: int, dry_run: bool):
    mode = "[DRY RUN] " if dry_run else ""
    print(f"\n{'─' * 50}")
    print(f"  {mode}Done!")
    print(f"  Emails sent this run : {len(sent)}")
    print(f"  Skipped (already sent): {skipped}")
    print(f"  Remaining quota today : {remaining_today}")
    if sent:
        print(f"\n  Sent to:")
        for s in sent:
            print(f"    - {s['contact_email']}  ({s['company_name']})")
    print(f"{'─' * 50}\n")


def check_and_sync_bounces(cfg: dict) -> list:
    """Connects to Gmail via IMAP, detects bounces using DSN parsing, classifies them,
    updates daily quotas for the actual send dates, and syncs to the Render tracker."""
    import imaplib
    import email as _email
    import re
    import urllib.request as _urllib
    import json as _json

    print("Checking Gmail for new bounces via IMAP...")
    email_addr = cfg["your_email"]
    app_pwd = cfg["app_password"].replace(" ", "")
    tracker_url = cfg.get("tracker_url", "").rstrip("/")

    # Persisted UID state
    state_path = cfg["log_path"].replace(".json", "_bounce_state.json")
    last_uid = 0
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as f:
                state_data = _json.load(f)
                last_uid = state_data.get("last_uid", 0)
        except Exception:
            pass

    bounced_records = []  # list of {email, reason, bounce_type, retry_after}

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_addr, app_pwd)
        mail.select("INBOX")

        # Search for messages since last processed UID
        status, messages = mail.uid(
            "search",
            None,
            f'(OR FROM "mailer-daemon" FROM "postmaster") UID {last_uid + 1}:*',
        )
        if status != "OK" or not messages[0].split():
            print("  No new bounce notification messages found.")
            mail.logout()
            return []

        message_uids = messages[0].split()
        print(f"  Scanning {len(message_uids)} new bounce notification emails...")

        max_uid = last_uid
        for uid_bytes in message_uids:
            msg_uid = int(uid_bytes)
            if msg_uid > max_uid:
                max_uid = msg_uid

            res, msg_data = mail.uid("fetch", uid_bytes, "(RFC822)")
            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue
                original_msg = _email.message_from_bytes(response_part[1])

                # Check for standard message/delivery-status DSN part
                dsn_part = None
                for part in original_msg.walk():
                    if part.get_content_type() == "message/delivery-status":
                        dsn_part = part
                        break

                bounced_email = None
                bounce_status = None
                bounce_action = None
                diagnostic_code = None

                if dsn_part:
                    try:
                        payload = dsn_part.get_payload()
                        if isinstance(payload, list):
                            for subpart in payload:
                                sub_msg = subpart
                                if sub_msg.get("Final-Recipient"):
                                    fr = sub_msg.get("Final-Recipient")
                                    parts = fr.split(";", 1)
                                    bounced_email = (
                                        parts[1].strip()
                                        if len(parts) > 1
                                        else parts[0].strip()
                                    )
                                    bounce_status = sub_msg.get("Status")
                                    bounce_action = sub_msg.get("Action")
                                    diagnostic_code = sub_msg.get("Diagnostic-Code")
                                    break
                        else:
                            payload_bytes = dsn_part.get_payload(decode=True)
                            blocks = payload_bytes.split(b"\n\n")
                            for block in blocks:
                                sub_msg = _email.message_from_bytes(block)
                                if sub_msg.get("Final-Recipient"):
                                    fr = sub_msg.get("Final-Recipient")
                                    parts = fr.split(";", 1)
                                    bounced_email = (
                                        parts[1].strip()
                                        if len(parts) > 1
                                        else parts[0].strip()
                                    )
                                    bounce_status = sub_msg.get("Status")
                                    bounce_action = sub_msg.get("Action")
                                    diagnostic_code = sub_msg.get("Diagnostic-Code")
                                    break
                    except Exception as pe:
                        print(f"    Error parsing DSN part: {pe}")

                # If no bounced_email was found via delivery-status, fall back to "To:" headers inside the body
                if not bounced_email:
                    body = ""
                    if original_msg.is_multipart():
                        for part in original_msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(
                                    errors="ignore"
                                )
                    else:
                        body = original_msg.get_payload(decode=True).decode(
                            errors="ignore"
                        )

                    to_matches = re.findall(
                        r"To:\s*(?:[^<\n]*?)\<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\>?",
                        body,
                        re.IGNORECASE,
                    )
                    if to_matches:
                        for m in to_matches:
                            if m.lower() != email_addr.lower():
                                bounced_email = m
                                break

                if bounced_email:
                    bounced_email = bounced_email.strip("<> ")
                    if bounced_email.lower() == email_addr.lower():
                        continue

                    # Classify status
                    bounce_type = "unknown"
                    if bounce_status:
                        status_clean = bounce_status.strip()
                        if status_clean.startswith("5"):
                            bounce_type = "hard"
                        elif status_clean.startswith("4"):
                            bounce_type = "soft"
                    elif bounce_action:
                        action_clean = bounce_action.strip().lower()
                        if action_clean == "failed":
                            bounce_type = "hard"
                        elif action_clean == "delayed":
                            bounce_type = "soft"

                    # Classify reason
                    reason = "Bounce — unknown reason"
                    if diagnostic_code:
                        diag_clean = diagnostic_code.strip()
                        parts = diag_clean.split(";", 1)
                        reason = (
                            parts[1].strip() if len(parts) > 1 else parts[0].strip()
                        )

                    # Classify retry_after
                    retry_after = ""
                    if bounce_type == "soft":
                        retry_after = "Temporary failure — Gmail will retry automatically for up to ~4 days before giving up"

                    bounced_records.append(
                        {
                            "email": bounced_email,
                            "reason": reason,
                            "bounce_type": bounce_type,
                            "retry_after": retry_after,
                        }
                    )

        mail.close()
        mail.logout()

        # Update persisted state with the max UID processed
        if max_uid > last_uid:
            try:
                with open(state_path, "w") as f:
                    _json.dump({"last_uid": max_uid}, f)
            except Exception as se:
                print(f"  Warning: Could not save bounce scan state: {se}")

    except Exception as e:
        print(f"  Warning: IMAP bounce check failed: {e}")
        return []

    if not bounced_records:
        print("  No new bounced addresses detected.")
        return []

    bounced_emails = [r["email"] for r in bounced_records]
    hard = sum(1 for r in bounced_records if r["bounce_type"] == "hard")
    soft = sum(1 for r in bounced_records if r["bounce_type"] == "soft")
    unknown = sum(1 for r in bounced_records if r["bounce_type"] == "unknown")
    print(
        f"  Detected {len(bounced_records)} bounces — {hard} hard, {soft} soft, {unknown} unknown: {bounced_emails}"
    )

    log = load_log(cfg["log_path"])
    failed = load_failed_log(cfg["log_path"])
    existing_failed = {item["email"] for item in failed}

    new_bounces_logged = 0

    for rec in bounced_records:
        email = rec["email"]

        if email not in log["sent"]:
            log["sent"].append(email)

        if email not in existing_failed:
            # A bounce means the message never landed, so it shouldn't keep
            # occupying a slot in the daily quota for the day it went out. The
            # decrement used to sit inside `if email not in log["sent"]`, which
            # is exactly backwards -- a bounce is by definition for something we
            # did send, so the address was already in `sent` and the branch never
            # ran. 495 of the entries in the failed log are in `sent`, so this
            # had effectively never fired.
            #
            # Gating the refund on "not already in existing_failed" makes it
            # idempotent for free: `failed` is persisted, so a re-scan of the
            # same bounce cannot refund the same day twice.
            sent_at = log.get("details", {}).get(email, {}).get("sent_at", "")
            sent_day = sent_at.split(" ")[0] if sent_at else None
            if sent_day and sent_day in log["daily"]:
                log["daily"][sent_day] = max(0, log["daily"][sent_day] - 1)

            failed.append(
                {
                    "email": email,
                    "name": "",
                    "company": log.get("details", {}).get(email, {}).get("company", ""),
                    "error": rec["reason"],
                    "kind": "bounce",
                    "bounce_type": rec["bounce_type"],
                    "retry_after": rec["retry_after"],
                    "time": utc_stamp(),
                }
            )
            existing_failed.add(email)
            new_bounces_logged += 1

            if tracker_url:
                try:
                    payload = _json.dumps(
                        {
                            "email": email,
                            "reason": rec["reason"],
                            "bounce_type": rec["bounce_type"],
                            "retry_after": rec["retry_after"],
                        }
                    ).encode("utf-8")
                    req = _urllib.Request(
                        f"{tracker_url}/api/log_bounce",
                        data=payload,
                        headers=tracker_headers(cfg, "application/json"),
                        method="POST",
                    )
                    with _urllib.urlopen(req, timeout=8):
                        pass
                except Exception as te:
                    print(f"    Tracker log_bounce failed for {email}: {te}")

    save_log(cfg["log_path"], log)

    failed_path = cfg["log_path"].replace(".json", "_failed.json")
    with open(failed_path, "w") as f:
        _json.dump(failed, f, indent=2)

    # Clean local SQLite database
    try:
        # Only remove hard bounces from DB — soft/unknown bounces may still be valid
        hard_emails = [
            r["email"] for r in bounced_records if r["bounce_type"] == "hard"
        ]
        if hard_emails:
            conn = sqlite3.connect(cfg["db_path"])
            cur = conn.cursor()
            cur.executemany(
                "DELETE FROM contacts WHERE email = ?", [(e,) for e in hard_emails]
            )
            conn.commit()
            deleted_count = conn.total_changes
            conn.close()
            print(
                f"  Removed {deleted_count} hard-bounced contacts from local database."
            )
    except Exception as dbe:
        print(f"  Warning: Database cleanup failed: {dbe}")

    print(f"  Successfully synced {new_bounces_logged} new bounces.")
    return bounced_emails



def move_to_wrong_address(db_path: str, contact: dict, service: str, reason: str):
    """Remove a contact from `contacts` and record it in `wrong_address` so it
    never resurfaces in a future CONTACT_QUERY."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO wrong_address
                (original_contact_id, company_id, company_name, name, role, email,
                 failed_service, invalid_reason, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contact["contact_id"],
                contact.get("company_id"),
                contact.get("company_name"),
                contact.get("contact_name"),
                contact.get("contact_role"),
                contact["contact_email"],
                service,
                reason,
                datetime.now().isoformat(),
            ),
        )
        cur.execute("DELETE FROM contacts WHERE id = ?", (contact["contact_id"],))
        conn.commit()
    finally:
        conn.close()


def hold_back_contact(db_path: str, contact_id: int, reason: str):
    """Excludes a contact from the send queue without discarding it -- unlike
    move_to_wrong_address, this is for addresses that couldn't be confirmed
    either way (catch-all domain, or SMTP gave no real answer), not addresses
    confirmed bad. Kept in `contacts` so it can be revisited manually or by a
    future verification pass; CONTACT_QUERY's `is_invalid = 0` filter keeps it
    out of the queue in the meantime."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE contacts SET is_invalid = 1, invalid_reason = ? WHERE id = ?",
            (reason, contact_id),
        )
        conn.commit()
    finally:
        conn.close()


def send_test_email(cfg: dict, recipient: str):
    """Send one rendered email to `recipient` and stop.

    Deliberately isolated from the campaign: it does not read the queue, does
    not consume daily quota, and writes nothing to the send log. The point is to
    see exactly what a contact receives -- HTML rendering, the resume
    attachment, the tracking pixel, the signed click links -- without a real
    address being involved."""
    if not cfg["your_email"] or not cfg["app_password"]:
        print("ERROR: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env.")
        sys.exit(1)

    if cfg.get("tracker_url") and not cfg.get("tracker_secret"):
        print(
            "  WARNING: TRACKER_SECRET is unset, so links will NOT be wrapped and "
            "the pixel will not record. Set it locally and on the tracker service "
            "first if you want to test tracking."
        )

    subject_template, body_template = load_template(cfg["template_path"])
    contact = {
        "contact_id": 0,
        "company_id": 0,
        "contact_email": recipient,
        "contact_name": cfg["your_name"],
        "contact_role": "Test Recipient",
        "company_name": "Test Company",
        "company_domain": "example.com",
        "company_industry": "",
        "funding_stage": "",
    }

    msg = build_email(cfg, contact, subject_template, body_template)
    print(f"\nTest send")
    print(f"  From    : {cfg['your_email']}")
    print(f"  To      : {recipient}")
    print(f"  Subject : {msg['Subject']}")

    html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode()
    tracker_url = cfg.get("tracker_url", "").rstrip("/")
    print(f"  Pixel   : {'yes' if tracker_url and f'{tracker_url}/t/' in html else 'no'}")
    print(f"  Links   : {'wrapped + signed' if tracker_url and f'{tracker_url}/c/' in html else 'not wrapped'}")
    print(f"  Resume  : {'attached' if any(p.get_filename() for p in msg.walk()) else 'MISSING'}")

    try:
        conn = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        conn.login(cfg["your_email"], cfg["app_password"].replace(" ", ""))
        conn.send_message(msg)
        conn.quit()
    except Exception as e:
        print(f"\n  FAILED: {e}")
        sys.exit(1)

    print(f"\n  SENT. Nothing was written to {cfg['log_path']} and no quota was used.")


def main():
    parser = argparse.ArgumentParser(description="Cold Mail Sender")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print emails without actually sending them",
    )
    parser.add_argument("--limit", type=int, help="Cap how many we send THIS run (never exceeds the daily quota)")
    parser.add_argument(
        "--verified-csv",
        metavar="PATH",
        help="CSV from `verifier -out` (email,verdict,...). Addresses with "
             "verdict=valid skip the live SMTP probe during this run.",
    )
    parser.add_argument(
        "--rate-per-hour",
        type=float,
        metavar="N",
        help="Spread sends evenly at N per hour (e.g. 15 sleeps 240s between sends). "
             "Paces the run so it does not look like a burst. Ignored in --dry-run.",
    )
    parser.add_argument(
        "--daily-limit",
        type=int,
        help="Override cfg['daily_limit'] for this run only (lets today's send count exceed the normal cap)",
    )
    parser.add_argument(
        "--show-queue",
        action="store_true",
        help="Show the next contacts that would be emailed and exit",
    )
    parser.add_argument(
        "--only-personalized",
        action="store_true",
        help="Only mail contacts that have a written email in PERSONALIZED_EMAILS "
             "or outreach/out/. Without this the queue is ordered by confidence "
             "and will reach different people than the batch was written for.",
    )
    parser.add_argument(
        "--check-bounces",
        action="store_true",
        help="Check Gmail for bounced emails, sync them to logs, clean DB, and exit",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        help="Read contacts from a local CSV instead of turso-full.db. The "
             "files in hr-database/ stay on this machine and are never pushed.",
    )
    parser.add_argument(
        "--test-send",
        metavar="EMAIL",
        help="Send one rendered email to EMAIL and exit. Touches no contact, "
             "writes nothing to the send log or quota. For verifying SMTP, HTML "
             "rendering, the attachment and the tracking links end to end.",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    # Addresses the Go verifier already confirmed deliverable. Only verdict
    # "valid" is trusted -- "unknown" means we could not tell, which is exactly
    # the case that still needs a live check.
    pre_verified = set()
    if args.verified_csv:
        import csv as _csv
        try:
            with open(args.verified_csv) as f:
                for row in _csv.DictReader(f):
                    if row.get("verdict", "").strip().lower() == "valid":
                        pre_verified.add(row["email"].strip().lower())
            print(f"  Pre-verified addresses loaded: {len(pre_verified)}")
        except Exception as e:
            print(f"  WARNING: could not read {args.verified_csv}: {e}")

    # Seconds to wait between sends. 15/hour -> 240s. Zero in dry-run, where
    # nothing leaves the machine and pacing would only waste your time.
    send_interval = (
        3600.0 / args.rate_per_hour
        if args.rate_per_hour and args.rate_per_hour > 0 and not dry_run
        else 0.0
    )
    cfg = CONFIG.copy()

    if args.daily_limit:
        cfg["daily_limit"] = args.daily_limit

    # Handle manual bounce check flag
    if args.check_bounces:
        check_and_sync_bounces(cfg)
        sys.exit(0)

    if args.test_send:
        send_test_email(cfg, args.test_send)
        sys.exit(0)

    # Validate config. These used to compare against the placeholder strings
    # "you@gmail.com" and "xxxx xxxx xxxx xxxx", which stopped existing when
    # CONFIG moved to os.environ.get(..., "") -- so a missing .env sailed past
    # both guards and surfaced as an opaque Gmail auth failure instead.
    if not dry_run:
        if not cfg["your_email"]:
            print("ERROR: GMAIL_ADDRESS is not set. Add it to .env before running.")
            sys.exit(1)
        if not cfg["app_password"]:
            print("ERROR: GMAIL_APP_PASSWORD is not set. Add it to .env before running.")
            print("  Generate one at: https://myaccount.google.com/apppasswords")
            sys.exit(1)
        if cfg["tracker_url"] and not cfg["tracker_secret"]:
            print(
                "  WARNING: TRACKER_URL is set but TRACKER_SECRET is not. Every "
                "tracker write will be rejected and no sends will be recorded."
            )

    # Automatically check for bounces on startup (if not a dry run)
    if not dry_run:
        check_and_sync_bounces(cfg)
        # Re-sync all local data to Render so dashboard survives server restarts
        sync_tracker_from_logs(cfg)

    # Load state
    log = load_log(cfg["log_path"])
    already_sent_today = sent_today(log)
    quota_left = cfg["daily_limit"] - already_sent_today

    # --limit caps how many we send THIS run (not the daily total)
    if args.limit:
        quota_left = min(quota_left, args.limit)

    print(f"\nCold Mail Sender")
    print(
        f"  Today's quota: {already_sent_today}/{cfg['daily_limit']} used  ->  {quota_left} left"
    )

    if quota_left <= 0:
        print("  Daily limit already reached. Come back tomorrow!")
        sys.exit(0)

    # Load template
    if not os.path.exists(cfg["template_path"]):
        print(f"ERROR: Template file not found: {cfg['template_path']}")
        sys.exit(1)
    subject_template, body_template = load_template(cfg["template_path"])
    sector_templates = load_sector_templates()
    if sector_templates:
        print(f"  Sector templates loaded: {', '.join(sorted(sector_templates))}")
    install_outreach_sends()
    if not subject_template:
        print("ERROR: Template missing 'Subject:' line on the first line.")
        sys.exit(1)

    # Everyone already contacted, from both sources of truth: the local send log
    # and any send_queue row the DB marked SENT/DONE. Previously the DB half of
    # this was applied by deleting those contact rows outright; it's a filter now.
    excluded = sent_index(log) | contacted_in_db(cfg["db_path"])

    # Fetch contacts
    if args.csv:
        if not os.path.exists(args.csv):
            print(f"ERROR: CSV not found: {args.csv}")
            sys.exit(1)
        all_contacts = load_contacts_from_csv(args.csv)
        print(f"  Source              : {args.csv}")
        print(f"  Total contacts in CSV: {len(all_contacts)}")
    else:
        all_contacts = fetch_contacts(cfg["db_path"])
        print(f"  Total contacts in DB: {len(all_contacts)}")

    # Filter out already-sent
    queue = [c for c in all_contacts if not already_sent(excluded, c["contact_email"])]
    print(f"  Unsent contacts     : {len(queue)}")

    # Restrict the run to contacts that have a hand-written or generated email.
    #
    # CONTACT_QUERY orders by confidence, while the batch generator ranks on
    # mailbox quality first, so the two disagree about who comes next: the
    # sender's first 30 and the 31 generated emails had zero addresses in
    # common. Without this flag a run intended to send researched mail sends
    # the generic template to a completely different set of people.
    if args.only_personalized:
        before = len(queue)
        queue = [c for c in queue
                 if c["contact_email"].strip().lower() in PERSONALIZED_EMAILS]
        print(f"  With a written email: {len(queue)}  (skipped {before - len(queue)})")

    if args.show_queue:
        print(f"\n  Next {min(quota_left, 20)} in queue:")
        for c in queue[:20]:
            print(
                f"    {c['contact_email']:40s} | {c['company_name']} | {c['contact_role']}"
            )
        sys.exit(0)

    # Take only what we're allowed today
    batch = queue[:quota_left]
    skipped_count = len(all_contacts) - len(queue)

    if not batch:
        print("  All contacts have been emailed!")
        sys.exit(0)

    # Send
    sent_this_run = []
    smtp_conn = None

    def smtp_connect():
        conn = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        conn.login(cfg["your_email"], cfg["app_password"].replace(" ", ""))
        return conn

    if not dry_run:
        try:
            smtp_conn = smtp_connect()
            print(f"  Gmail SMTP connected\n")
        except Exception as e:
            print(f"ERROR: Gmail login failed: {e}")
            print(
                "  Make sure you're using an App Password, not your regular password."
            )
            print("  Generate one at: https://myaccount.google.com/apppasswords")
            sys.exit(1)

    # In dry-run we just preview the top of the queue. In a real run, invalid
    # addresses get filtered out live by the verification waterfall, so we walk
    # the full priority-ordered queue and keep going until quota_left contacts
    # have actually been sent to (or the queue runs out) — otherwise skipped
    # invalids would silently shrink today's real send count below the daily cap.
    candidates = batch if dry_run else queue

    try:
        for i, contact in enumerate(candidates, 1):
            if not dry_run and len(sent_this_run) >= quota_left:
                break

            email_addr = contact["contact_email"]
            company = contact["company_name"]
            role = contact["contact_role"] or "—"

            progress = f"{len(sent_this_run) + 1:02d}/{quota_left}" if not dry_run else f"{i:02d}/{len(batch)}"
            print(f"  [{progress}] {email_addr:40s} | {company} | {role}")

            if not dry_run:
                try:
                    # Addresses the Go verifier already confirmed are skipped
                    # here. Re-probing them wastes a round trip, and at volume
                    # Google starts answering 4.2.1 "receiving mail at a rate
                    # that prevents additional messages" to EVERY probe from
                    # this IP -- which reads as unverifiable and silently holds
                    # back good contacts. One bulk verification pass should not
                    # poison the send that follows it.
                    if email_addr.lower() in pre_verified:
                        is_valid, checked_by = True, "pre_verified"
                        print(f"         pre-verified, skipping live probe")
                    else:
                        is_valid, checked_by = verify_email(email_addr)
                except Exception as ver_err:
                    # Verification is a best-effort side check, never a reason to
                    # abort the run. A UnicodeEncodeError on one accented address
                    # used to take down the whole batch on contact 1 of 15.
                    # Unknown verdict means hold back, so nothing is discarded.
                    print(f"         Verifier crashed ({type(ver_err).__name__}: {ver_err}). Holding back.")
                    is_valid, checked_by = None, "verifier_error"
                # contact_id is None for CSV-sourced rows -- there is no database
                # row behind them, so record the outcome but skip the DB writes.
                from_db = contact.get("contact_id") is not None
                if is_valid is False:
                    print(f"         Invalid ({checked_by}). Skipping.")
                    record_failed(cfg["log_path"], contact, f"Failed verification: {checked_by}", kind="skip")
                    if from_db:
                        move_to_wrong_address(cfg["db_path"], contact, checked_by, "failed verification")
                    continue
                if is_valid is None:
                    # Unverifiable (catch-all domain, or SMTP gave no real answer) --
                    # hold back rather than send or discard. Kept in `contacts` so
                    # it can be revisited later, just excluded from the queue for now.
                    print(f"         Unverifiable ({checked_by}). Holding back, not sending.")
                    record_failed(cfg["log_path"], contact, f"Failed verification: {checked_by}", kind="skip")
                    if from_db:
                        hold_back_contact(cfg["db_path"], contact["contact_id"], checked_by)
                    continue


            if dry_run:
                personalized = PERSONALIZED_EMAILS.get(email_addr)
                if personalized:
                    subject = personalized["subject"]
                    body = personalized["body"]
                    mode_tag = "[PERSONALIZED]"
                else:
                    subj_t, body_t, mode_tag = pick_template(
                        contact, sector_templates, (subject_template, body_template)
                    )
                    subject = render(subj_t, contact)
                    body = render(body_t, contact)
                print(f"         {mode_tag}")
                print(f"         Subject : {subject}")
                print(f"         Body preview: {body[:120].strip()}...")
                print()
                sent_this_run.append(
                    contact
                )  # track for summary display only, do NOT write to log
                continue

            try:
                subj_t, body_t, mode_tag = pick_template(
                    contact, sector_templates, (subject_template, body_template)
                )
                print(f"         {mode_tag}")
                msg = build_email(cfg, contact, subj_t, body_t)
                try:
                    smtp_conn.send_message(msg)
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                        OSError) as conn_err:
                    # One socket for up to daily_limit sends, with no recovery,
                    # meant a single mid-run disconnect failed every remaining
                    # contact. Those got written to the failed log, which
                    # check_and_sync_bounces dedupes against -- so they were
                    # never retried and showed on the dashboard as bounces.
                    # A transport drop says nothing about the address, so
                    # reconnect once and retry before calling it a failure.
                    print(f"         SMTP dropped ({conn_err}); reconnecting…")
                    try:
                        smtp_conn.quit()
                    except Exception:
                        pass
                    smtp_conn = smtp_connect()
                    smtp_conn.send_message(msg)
                record_sent(log, email_addr, company)
                sent_this_run.append(contact)
                save_log(
                    cfg["log_path"], log
                )  # Save after each send (safe against crashes)

                # Notify tracking server of successful send
                tracker_url = cfg.get("tracker_url", "").rstrip("/")
                if tracker_url:
                    try:
                        import urllib.request as _urllib
                        import json as _json

                        req = _urllib.Request(
                            f"{tracker_url}/api/log_send",
                            data=_json.dumps(
                                {"email": email_addr, "company": company}
                            ).encode("utf-8"),
                            headers=tracker_headers(cfg, "application/json"),
                            method="POST",
                        )
                        with _urllib.urlopen(req, timeout=5) as resp:
                            pass
                    except Exception as te:
                        print(f"         Tracker API Log Warning: {te}")

                print(f"         SENT")

                # Pace the run. Gmail tolerates bursts, but recipient servers and
                # spam filters treat a rapid identical-template burst from one
                # sender as exactly what it looks like. Sleeping between sends
                # spreads the run out; --rate-per-hour sets the pace.
                if send_interval and len(sent_this_run) < quota_left:
                    print(f"         waiting {send_interval:.0f}s (next at "
                          f"{datetime.now().strftime('%H:%M:%S')} + {send_interval:.0f}s)")
                    time.sleep(send_interval)
            except Exception as e:
                print(f"         FAILED: {e}")
                record_failed(cfg["log_path"], contact, e)  # Log to failed_log.json

    finally:
        if smtp_conn:
            smtp_conn.quit()

    # Dry run never writes to the log — nothing was actually sent

    remaining = cfg["daily_limit"] - sent_today(log)
    print_summary(sent_this_run, skipped_count, remaining, dry_run)


if __name__ == "__main__":
    main()
