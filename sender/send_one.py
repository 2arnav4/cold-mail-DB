#!/usr/bin/env python3
"""send_one.py -- send exactly one real email from the batch to a chosen address.

`--test-send` renders a placeholder ("Test Company", "Test Recipient") using the
default template, which answers a different question: it proves the render path
works, not that the mail a specific contact receives is correct. This sends one
of the actual generated batch emails, byte for byte, to an address of your
choosing, so what lands in the inbox is what the real recipient would get.

It also registers the send with the tracker before sending. The `/t/*.gif`
endpoint deliberately refuses to create rows -- otherwise anyone could append to
`sends`, and `total_sent` is the denominator of the open and bounce rates -- so
without a prior `log_send` the pixel loads and records nothing.

Nothing is written to sent_log.json and no daily quota is consumed, so the real
recipient stays in the queue for a genuine send later.

Usage:
  python3 send_one.py --to you@gmail.com --from-batch coval.dev
  python3 send_one.py --to you@gmail.com --from-batch coval.dev --dry-run
"""
import argparse
import json
import os
import smtplib
import sys
import urllib.request

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import send_emails as se  # noqa: E402


def load_batch_email(domain: str) -> dict:
    """Read the generated send.json for one company."""
    slug = domain.lower().replace(".", "-")
    path = os.path.join(HERE, "outreach", "out", slug, "send.json")
    if not os.path.exists(path):
        avail = sorted(os.listdir(os.path.join(HERE, "outreach", "out")))
        raise SystemExit(f"no send.json for {domain}\navailable: {', '.join(avail)}")
    return json.load(open(path))


def register_with_tracker(cfg: dict, email: str, company: str) -> bool:
    url = cfg.get("tracker_url", "").rstrip("/")
    if not url:
        print("  tracker: TRACKER_URL not set, skipping registration")
        return False
    try:
        req = urllib.request.Request(
            f"{url}/api/log_send",
            data=json.dumps({"email": email, "company": company}).encode(),
            headers=se.tracker_headers(cfg, "application/json"),
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  tracker: registered ({r.status})")
            return True
    except Exception as e:
        print(f"  tracker: registration FAILED -- {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True)
    ap.add_argument("--from-batch", required=True,
                    help="company domain whose generated email to send, e.g. coval.dev")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = se.CONFIG
    if not cfg["your_email"] or not cfg["app_password"]:
        raise SystemExit("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing from .env")

    payload = load_batch_email(args.from_batch)
    company = args.from_batch

    # build_email looks PERSONALIZED_EMAILS up by recipient, so the chosen
    # company's copy is registered against the address actually being mailed.
    # The greeting stays as the real contact's, which is the point: this shows
    # what that contact receives, not a rewritten version of it.
    se.install_outreach_sends()
    se.PERSONALIZED_EMAILS[args.to.lower()] = {
        "subject": payload["subject"], "body": payload["body"]}
    # The target address may already own a tier-one deck from outreach/ --
    # outreach/out/tarrakki/send.json is addressed to the personal inbox, so a
    # Coval email sent there arrived carrying the Tarrakki deck. Clear it, so
    # this is a clean copy of the batch email and nothing else.
    se.OUTREACH_DECKS[args.to.lower()] = None

    contact = {
        "contact_id": 0, "company_id": 0,
        "contact_email": args.to,
        "contact_name": "", "contact_role": "",
        "company_name": company, "company_domain": company,
        "company_industry": "", "funding_stage": "",
    }
    # build_email already sets To from contact_email; setting it again appends a
    # second To header rather than replacing it, and a duplicate To is itself a
    # spam signal.
    msg = se.build_email(cfg, contact, payload["subject"], payload["body"])

    print(f"\n  From    : {cfg['your_email']}")
    print(f"  To      : {args.to}")
    print(f"  Subject : {msg['Subject']}")
    print(f"  Copy of : {payload['recipient_email']} ({company})")
    atts = [p.get_filename() for p in msg.walk() if p.get_filename()]
    print(f"  Attach  : {atts or 'none (resume linked in body)'}")
    html = "".join(p.get_payload(decode=True).decode("utf8", "ignore")
                   for p in msg.walk() if p.get_content_type() == "text/html")
    tr = cfg.get("tracker_url", "").rstrip("/")
    print(f"  Pixel   : {'yes' if tr and f'{tr}/t/' in html else 'no'}")

    if args.dry_run:
        print("\n  dry run, nothing sent")
        return 0

    register_with_tracker(cfg, args.to, company)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(cfg["your_email"], cfg["app_password"])
        s.sendmail(cfg["your_email"], [args.to], msg.as_string())
    print("\n  SENT")
    print("  nothing written to sent_log.json; the real recipient stays queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
