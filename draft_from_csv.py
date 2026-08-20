#!/usr/bin/env python3
"""draft_from_csv.py -- draft personalized emails for an explicit contact list.

daily_drafts.py picks its own targets out of the database by confidence and
provenance. That is right for the daily run, and wrong when the list has
already been decided -- one contact per company, chosen by hand, some of them
freshly probed and not yet re-scored in the database.

So this reuses daily_drafts wholesale (research, ask, compose, validate, the
retry-at-a-higher-temperature loop and the duplicate-subject guard) and swaps
out only the target selection: the contacts come from a CSV instead of a
SELECT. The output file is the same shape daily_drafts writes, so preview_batch
and send_emails read it unchanged.

Usage:
  ./venv/bin/python draft_from_csv.py --csv batch-17-20260820.csv \
      --out drafts-batch17-2026-08-20.json
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import send_emails as se          # noqa: E402
import daily_drafts as dd         # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="contacts CSV (email,name,title,company,domain)")
    ap.add_argument("--out", required=True, help="output drafts JSON")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    targets = se.load_contacts_from_csv(args.csv)
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        return print(f"Nothing to draft in {args.csv}.") or 1

    print(f"Drafting {len(targets)} emails from {args.csv}")
    client = dd._client()
    con = sqlite3.connect(se.CONFIG["db_path"], timeout=60)
    seen = dd.past_subjects()
    drafts, failed = {}, []

    for i, c in enumerate(targets, 1):
        email = c["contact_email"].strip().lower()
        blurb = dd.research(c, con, False)
        subject = None
        for attempt, temp in enumerate((0.7, 0.9), 1):
            try:
                subject, opener = dd.ask(client, c, blurb, temp)
            except Exception as e:
                print(f"  [{i:02d}/{len(targets)}] {email:34} GROQ FAILED: {e}")
                subject = None
                continue
            body = dd.compose(c, subject, opener)
            err = dd.validate(subject, body, seen)
            if err is None:
                drafts[email] = {"subject": subject, "body": body}
                seen.add(subject.strip().lower())
                print(f"  [{i:02d}/{len(targets)}] {email:34} {c['company_name'][:18]:18} OK")
                break
            print(f"  [{i:02d}/{len(targets)}] {email:34} retry {attempt}: {err}")
            subject = None
        if subject is None:
            failed.append(email)

    con.close()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(drafts, fh, indent=2, ensure_ascii=False)
    print(f"\n  written: {len(drafts)}   failed: {len(failed)}")
    for f in failed:
        print(f"    dropped: {f}")
    print(f"  -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
