#!/usr/bin/env python3
"""clean_sent_pixels.py -- strip the tracking pixel from your own Sent copies.

Reading your own Sent folder registers as the recipient opening the email, and
no check on the incoming request can prevent it. Gmail never loads the pixel
from the reader's browser; it proxies every image through its own servers, so
the hit arrives from 66.249.x.x with a ggpht.com user agent whether the reader
is the recipient or you. The Sent copy and the delivered copy carry the same
URL. Nothing in the request separates them.

So stop trying to detect it. The reason opening your Sent folder records
anything is that Gmail filed a copy containing the pixel. Remove the pixel from
*that copy only* and the problem is gone at the source: your copy has nothing
to load, and the recipient's copy is untouched because it left the building
before this runs.

How: fetch each Sent message, rewrite the text/html part without the `<img>`,
APPEND the result back to Sent preserving the original date and flags, then move
the original to Trash. Gmail's IMAP cannot edit a message in place.

Safe to re-run: messages with no pixel are skipped, so a second pass is a no-op.

Usage:
  python3 sender/clean_sent_pixels.py --since 06-Aug-2026            # report
  python3 sender/clean_sent_pixels.py --since 06-Aug-2026 --apply
  python3 sender/clean_sent_pixels.py --since 06-Aug-2026 --apply --limit 1
"""
import argparse
import email
import imaplib
import os
import re
import sys
from email.utils import parsedate_tz, mktime_tz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import send_emails as se  # noqa: E402

SENT = '"[Gmail]/Sent Mail"'
PIXEL_RE_TMPL = r'<img[^>]*src="[^"]*{host}/t/[^"]*"[^>]*>'


def strip_pixel(msg, host: str) -> bool:
    """Remove the tracking img from every text/html part. True if anything changed."""
    pattern = re.compile(PIXEL_RE_TMPL.format(host=re.escape(host)), re.I)
    changed = False
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        html = payload.decode("utf-8", "ignore")
        new = pattern.sub("", html)
        if new != html:
            charset = part.get_content_charset() or "utf-8"
            # set_payload with a charset re-encodes and fixes the
            # Content-Transfer-Encoding header, which matters because these
            # parts arrive base64 and a raw string assignment would leave the
            # header claiming base64 over plain text.
            del part["Content-Transfer-Encoding"]
            part.set_payload(new, charset=charset)
            changed = True
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=None,
                    help="IMAP date, e.g. 06-Aug-2026. Default: today")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cfg = se.CONFIG
    if not cfg["your_email"] or not cfg["app_password"]:
        raise SystemExit("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing from .env")
    tracker = cfg.get("tracker_url", "").rstrip("/")
    if not tracker:
        raise SystemExit("TRACKER_URL is not set; nothing to strip")
    host = tracker.split("//", 1)[-1]

    since = args.since or __import__("datetime").date.today().strftime("%d-%b-%Y")

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(cfg["your_email"], cfg["app_password"])
    M.select(SENT)
    typ, data = M.search(None, "SINCE", since)
    ids = data[0].split()
    if args.limit:
        ids = ids[-args.limit:]
    print(f"{len(ids)} message(s) in Sent since {since}\n")

    cleaned = skipped = failed = 0
    for i, mid in enumerate(ids, 1):
        typ, d = M.fetch(mid, "(RFC822 INTERNALDATE FLAGS)")
        if typ != "OK" or not d or not isinstance(d[0], tuple):
            failed += 1
            continue
        meta = d[0][0].decode("utf-8", "ignore")
        msg = email.message_from_bytes(d[0][1])
        subject = (msg["Subject"] or "")[:48]

        if not strip_pixel(msg, host):
            skipped += 1
            continue

        print(f"  [{i}/{len(ids)}] {subject}")
        if not args.apply:
            cleaned += 1
            continue

        # Preserve when it was sent, so the Sent folder keeps its order.
        internal = imaplib.Internaldate2tuple(meta.encode()) if "INTERNALDATE" in meta else None
        flags = ""
        fm = re.search(r"FLAGS \(([^)]*)\)", meta)
        if fm:
            flags = " ".join(f for f in fm.group(1).split() if f.lower() != r"\recent")

        # Append first, confirm it arrived, and only then remove the original.
        # The first version deleted first and trusted the append; the append
        # failed silently and the message existed nowhere until it was fetched
        # back out of Bin. Never destroy the only copy on the assumption that
        # the replacement worked.
        msg_id = (msg["Message-ID"] or "").strip()
        try:
            typ, _ = M.append(SENT, flags, internal, msg.as_bytes())
            if typ != "OK":
                print("      append refused, original left untouched")
                failed += 1
                continue
        except Exception as e:
            print(f"      append failed ({e}), original left untouched")
            failed += 1
            continue

        # Verify by Message-ID: two copies should now exist, the clean one and
        # the original. Without this an append that "succeeded" but did not
        # land still loses the message.
        try:
            typ, chk = M.search(None, f'(HEADER Message-ID "{msg_id}")')
            copies = len(chk[0].split()) if chk and chk[0] else 0
        except Exception:
            copies = 0
        if not msg_id or copies < 2:
            print(f"      replacement not confirmed ({copies} copies), original kept")
            failed += 1
            continue

        try:
            M.store(mid, "+X-GM-LABELS", "\\Trash")
            M.store(mid, "+FLAGS", "\\Deleted")
            cleaned += 1
        except Exception as e:
            print(f"      could not retire the original: {e}")
            failed += 1

    if args.apply and cleaned:
        M.expunge()
    M.logout()

    print(f"\n  pixel removed : {cleaned}")
    print(f"  already clean : {skipped}")
    if failed:
        print(f"  failed        : {failed}")
    if not args.apply:
        print("\n  dry run, nothing changed. re-run with --apply")
    else:
        print("\n  your Sent copies no longer load the pixel;")
        print("  the recipients' copies were delivered before this ran and are untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
