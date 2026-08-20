#!/usr/bin/env python3
"""preview_batch.py -- mail a batch to yourself before it reaches a founder.

A cold email is unrecallable. Reading a draft in a terminal is not the same as
seeing what lands in an inbox: the terminal shows neither the HTML rendering,
nor the link targets after the tracker rewrites them, nor what the subject
looks like next to real mail.

So this builds each message through send_emails.build_email -- the same
function the real send uses, not a reimplementation of it -- and delivers the
result to PREVIEW_TO instead of the contact. If the preview looks right, the
batch is right, because it is the same code path.

Nothing here touches sent_log.json, the daily quota, or the contacts database.
Previewing a batch twice costs nothing.

The message is sent byte-for-byte as the contact would receive it, with one
exception: a marker line is appended naming the intended recipient, because
five previews of five different founders are otherwise indistinguishable in
an inbox. It sits below the signature, after a separator, so it is obvious it
is not part of the letter. Pass --no-marker to send the email completely
untouched.

Usage:
  ./venv/bin/python preview_batch.py --drafts drafts-2026-08-19.json
  ./venv/bin/python preview_batch.py --drafts d.json --limit 3
  ./venv/bin/python preview_batch.py --csv lolo.csv --no-marker
"""
import argparse
import csv as _csv
import json
import os
import smtplib
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import send_emails as se  # noqa: E402  (loads .env on import)

PREVIEW_TO = "singlaarnav2405@gmail.com"


def reencode(part, text: str) -> None:
    """Replace a MIME part's body, keeping the headers honest about it.

    `set_payload(text, charset="utf-8")` looks like it does this, but
    set_charset() only base64-encodes the body when the part has no
    Content-Transfer-Encoding header yet. MIMEText already set one, so the
    encode step is skipped: the header keeps saying `base64` while the payload
    goes back to being raw text.

    Gmail hides the damage -- it notices the body is not valid base64 and falls
    back to rendering it raw. Zoho believes the header, decodes plain prose as
    base64, and shows a wall of mojibake it then offers to translate from
    Icelandic. Deleting the header first lets set_payload re-encode properly,
    so both clients see the same thing.
    """
    del part["Content-Transfer-Encoding"]
    part.set_payload(text, charset="utf-8")


def contacts_from_csv(path: str) -> list:
    return se.load_contacts_from_csv(path)


def contacts_from_drafts(path: str) -> list:
    """Build minimal contacts from a drafts file.

    daily_drafts.py writes {email: {subject, body}} and nothing else, so the
    company name has to be recovered from the database when it is there. A
    missing company is not fatal: it only affects the marker line and the
    tracking pixel payload, never the letter itself.
    """
    drafts = json.load(open(path, encoding="utf-8"))
    lookup = {}
    try:
        import sqlite3
        con = sqlite3.connect(se.CONFIG["db_path"])
        for email, company in con.execute(
                "SELECT LOWER(email), COALESCE(company_name, '') FROM contacts"):
            lookup[email] = company
        con.close()
    except Exception:
        pass  # a drafts-only preview is still worth having

    out = []
    for email in drafts:
        e = email.strip().lower()
        out.append({"contact_email": e,
                    "contact_name": "",
                    "company_name": lookup.get(e, ""),
                    "company_sector": "",
                    "contact_title": ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--drafts", metavar="PATH", help="a drafts-<date>.json")
    src.add_argument("--csv", metavar="PATH", help="a contacts CSV")
    ap.add_argument("--to", default=PREVIEW_TO, help=f"where to send (default {PREVIEW_TO})")
    ap.add_argument("--limit", type=int, default=0, help="preview only the first N")
    ap.add_argument("--no-marker", action="store_true",
                    help="send byte-for-byte, with no 'preview for ...' line")
    args = ap.parse_args()

    cfg = se.CONFIG
    if not cfg["your_email"] or not cfg["app_password"]:
        print("ERROR: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env.")
        return 1

    if args.drafts:
        contacts = contacts_from_drafts(args.drafts)
        source = args.drafts
    else:
        contacts = contacts_from_csv(args.csv)
        source = args.csv
    if args.limit:
        contacts = contacts[:args.limit]
    if not contacts:
        print(f"Nothing to preview in {source}.")
        return 1

    sector_templates = se.load_sector_templates()
    subject_template, body_template = se.load_template(cfg["template_path"])

    print("Preview batch")
    print(f"  Source  : {source}")
    print(f"  From    : {cfg['your_email']}")
    print(f"  To      : {args.to}")
    print(f"  Count   : {len(contacts)}")
    print()

    try:
        conn = smtplib.SMTP_SSL(cfg["smtp_host"], 465, timeout=30)
        conn.login(cfg["your_email"], cfg["app_password"].replace(" ", ""))
    except Exception as e:
        print(f"ERROR: SMTP login failed: {e}")
        return 1

    sent = 0
    for i, contact in enumerate(contacts, 1):
        addr = contact["contact_email"]
        subj_t, body_t, mode = se.pick_template(
            contact, sector_templates, (subject_template, body_template))
        personalized = se.PERSONALIZED_EMAILS.get(addr)
        tag = "[PERSONALIZED]" if personalized else mode

        msg = se.build_email(cfg, contact, subj_t, body_t)

        if not args.no_marker:
            marker = (f"\n\n----------\npreview for {addr}"
                      f"{' at ' + contact['company_name'] if contact.get('company_name') else ''}")
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True).decode("utf-8", "replace")
                    reencode(part, payload + marker)
                elif part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True).decode("utf-8", "replace")
                    reencode(part, payload.replace(
                        "</body>", f"<hr><p>{marker.strip()}</p></body>")
                        if "</body>" in payload
                        else payload + f"<hr><p>{marker.strip()}</p>")

        # The preview goes to the operator, never to the contact. Rewriting the
        # header without rebuilding the message keeps subject, body, links and
        # attachments exactly as the contact would have received them.
        del msg["To"]
        msg["To"] = args.to

        try:
            conn.send_message(msg)
            print(f"  [{i:02d}/{len(contacts)}] {addr:34} {tag:16} {msg['Subject'][:40]}")
            sent += 1
        except Exception as e:
            print(f"  [{i:02d}/{len(contacts)}] {addr:34} FAILED: {e}")
        if i < len(contacts):
            time.sleep(2)

    conn.quit()
    print(f"\n  {sent} preview(s) delivered to {args.to}.")
    print("  Nothing was written to sent_log.json and no quota was used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
