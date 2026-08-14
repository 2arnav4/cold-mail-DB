#!/usr/bin/env python3
"""send_drafts.py -- load hand-written drafts into the sender, or preview one.

Two jobs, and neither of them needs an assistant in the loop:

  --preview EMAIL     render one draft and send it to EMAIL. Writes nothing:
                      no send log, no quota, no contact row touched. This is
                      how you check the copy, the HTML, the tracking pixel and
                      the resume link before a real recipient sees any of it.

  --load              merge a drafts file into personalized_emails.local.json,
                      which is what send_emails.py reads. After this,
                      `send_emails.py --only-personalized` mails exactly these
                      people and nobody else.

The drafts file is {"email": {"subject": ..., "body": ...}}, the same shape
personalized_emails.local.json already uses.

Why preview goes through send_emails.build_email rather than composing its own
message: the pixel, the markdown-to-HTML pass and the multipart layout all live
there. A preview built any other way would be testing a different email than
the one that ships, which makes it worse than no preview at all.

Usage:
  python3 -m sender.send_drafts --preview me@gmail.com --pick algolia
  python3 -m sender.send_drafts --load drafts.json --dry-run
  python3 -m sender.send_drafts --load drafts.json
"""
import argparse
import json
import os
import smtplib
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import send_emails as S  # noqa: E402  (loads .env into os.environ on import)

LIVE = os.path.join(HERE, "personalized_emails.local.json")


def load_drafts(path):
    with open(path, encoding="utf-8") as fh:
        drafts = json.load(fh)
    if not isinstance(drafts, dict) or not drafts:
        sys.exit(f"{path} is not a non-empty object of email -> {{subject, body}}")
    return drafts


def pick(drafts, needle):
    """Choose one draft by a substring of its address or its greeting line."""
    if not needle:
        return next(iter(drafts.items()))
    n = needle.lower()
    for email, d in drafts.items():
        if n in email.lower() or n in d["subject"].lower() or n in d["body"][:80].lower():
            return email, d
    sys.exit(f"no draft matches {needle!r}. Available: {', '.join(drafts)}")


def preview(recipient, email, draft):
    """Send one draft to `recipient`, exactly as the real one would render.

    The draft is injected into PERSONALIZED_EMAILS in memory rather than written
    to disk, so previewing never leaves a half-loaded entry behind that a later
    campaign run would pick up.
    """
    cfg = S.CONFIG
    if not cfg["your_email"] or not cfg["app_password"]:
        sys.exit("GMAIL_ADDRESS / GMAIL_APP_PASSWORD are not set in .env")

    S.PERSONALIZED_EMAILS[recipient] = {"subject": draft["subject"], "body": draft["body"]}
    company = draft["body"].split("\n")[0].replace("Hi ", "").rstrip(",") or "Preview"

    contact = {
        "contact_id": 0, "company_id": 0,
        "contact_email": recipient,
        # The greeting comes from the draft body itself, so these only matter
        # for the tracking token and any template fallback.
        "contact_name": company, "contact_role": "",
        "company_name": email.split("@")[-1].split(".")[0].title(),
        "company_domain": email.split("@")[-1],
        "company_industry": "", "funding_stage": "",
    }
    msg = S.build_email(cfg, contact, "{{subject}}", "{{body}}")

    html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode()
    public = cfg.get("tracker_public_url", "").rstrip("/")

    print(f"\nPreview of the {contact['company_name']} draft")
    print(f"  From    : {cfg['your_email']}")
    print(f"  To      : {recipient}")
    print(f"  Subject : {msg['Subject']}")
    # Checked against tracker_public_url, not tracker_url. The mail embeds the
    # public hostname on purpose, so testing for the API hostname reports a
    # missing pixel on a message that carries one.
    print(f"  Pixel   : {'yes' if public and f'{public}/t/' in html else 'NO'}")
    print(f"  Resume  : {'linked in body' if 'resume.pdf' in html else 'NOT LINKED'}")
    print(f"  Attach  : {'yes' if any(p.get_filename() for p in msg.walk()) else 'no (linked instead)'}")

    conn = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
    conn.login(cfg["your_email"], cfg["app_password"].replace(" ", ""))
    conn.send_message(msg)
    conn.quit()
    print(f"\n  SENT. Nothing written to {os.path.basename(cfg['log_path'])}, no quota used.")


def load_into_live(drafts, dry):
    live = {}
    if os.path.exists(LIVE):
        with open(LIVE, encoding="utf-8") as fh:
            live = json.load(fh)

    # A hand-written entry already in the live file wins. Overwriting one with a
    # generated draft silently discards copy somebody wrote on purpose, and the
    # loss is invisible until the wrong email has already gone out.
    new = {k: v for k, v in drafts.items() if k not in live}
    clash = [k for k in drafts if k in live]

    print(f"{len(drafts)} drafts · {len(new)} new · {len(clash)} already present (kept as-is)")
    for k in clash:
        print(f"  keeping existing: {k}")
    for k in new:
        print(f"  + {k}")

    if dry:
        print("\n(dry run, nothing written)")
        return
    live.update(new)
    with open(LIVE, "w", encoding="utf-8") as fh:
        json.dump(live, fh, indent=2, ensure_ascii=False)
    print(f"\n{LIVE} now holds {len(live)} written emails")
    print("send them with:  ./run_batch.sh 15")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drafts", default=None, help="path to the drafts JSON")
    ap.add_argument("--preview", metavar="EMAIL", help="send one draft here and stop")
    ap.add_argument("--pick", default=None, help="substring choosing which draft to preview")
    ap.add_argument("--load", action="store_true", help="merge the drafts into the live file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.drafts:
        sys.exit("--drafts is required")
    drafts = load_drafts(args.drafts)

    if args.preview:
        email, draft = pick(drafts, args.pick)
        preview(args.preview, email, draft)
    elif args.load:
        load_into_live(drafts, args.dry_run)
    else:
        sys.exit("nothing to do: pass --preview EMAIL or --load")


if __name__ == "__main__":
    main()
