#!/usr/bin/env python3
"""promote_best.py — pick the contacts most likely to actually land and raise
their priority so they go out first.

Ranking, highest first:
  1. Verifier verdict. `valid` means the recipient's mail server accepted RCPT
     TO for that exact address -- the only hard evidence of deliverability we
     can get. `unknown` means we could not tell, usually because our sending IP
     is on Spamhaus, and is used only to fill the list if valids run short.
     `invalid` is never promoted.
  2. Stage and recency. Newly funded early-stage startups are both likelier to
     be hiring and likelier to read founder-addressed mail. Recent YC batches
     rank above a bare funding stage because the batch label is dated.
  3. Sector match, so the contact gets a targeted template rather than the
     generic one.

Usage:
  python3 promote_best.py verified.csv --top 15
  python3 promote_best.py verified.csv --top 15 --apply
"""
import csv
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "turso-full.db")
PROMOTED_PRIORITY = 6000  # above the existing 5000 ceiling

VERDICT_RANK = {"valid": 0, "unknown": 1}
BATCH_RANK = {"S25": 0, "W25": 1, "S24": 2, "W24": 3, "S23": 4, "W23": 5}
STAGE_RANK = {"pre-seed": 0, "seed": 1, "series-a": 2}


def sent_emails() -> set:
    path = os.path.join(HERE, "sent_log.json")
    if not os.path.exists(path):
        return set()
    return {e.lower() for e in json.load(open(path)).get("sent", [])}


def sort_key(row) -> tuple:
    # "other" is a real classification but it routes to the generic template,
    # so rank it below a matched sector and above no sector at all.
    sector = (row["sector"] or "").lower()
    sector_rank = 0 if sector and sector != "other" else (1 if sector else 2)
    return (
        VERDICT_RANK.get(row["verdict"], 9),
        sector_rank,
        BATCH_RANK.get(row["batch"] or "", 8),
        STAGE_RANK.get((row["fs"] or "").lower(), 8),
        row["email"],
    )


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    csv_path = args[0]
    apply = "--apply" in sys.argv
    top = 15
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])

    verdicts = {}
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            verdicts[r["email"].strip().lower()] = r["verdict"].strip().lower()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    already = sent_emails()

    rows = []
    for r in con.execute("""
        SELECT ct.id, ct.email, ct.name, ct.role, co.name AS cname,
               co.funding_stage AS fs, co.batch, co.sector
        FROM contacts ct JOIN companies co ON co.id = ct.company_id
        WHERE ct.is_invalid = 0 AND ct.email IS NOT NULL AND TRIM(ct.email) <> ''
    """):
        email = r["email"].lower()
        verdict = verdicts.get(email)
        if verdict not in ("valid", "unknown") or email in already:
            continue
        # Skip non-ASCII local parts. smtplib encodes SMTP commands as ASCII
        # unless SMTPUTF8 is negotiated, so send_emails.py holds these back --
        # promoting one would waste a slot in the batch.
        try:
            email.encode("ascii")
        except UnicodeEncodeError:
            continue
        d = dict(r)
        d["verdict"] = verdict
        rows.append(d)

    rows.sort(key=sort_key)

    # One contact per company. These are mostly seed-stage startups with a
    # handful of people; three founders at the same company receiving the same
    # template on the same day reads as spam, and it burns three slots for one
    # shot at one company.
    chosen, seen_companies = [], set()
    for r in rows:
        key = (r["cname"] or r["email"].split("@")[-1]).strip().lower()
        if key in seen_companies:
            continue
        seen_companies.add(key)
        chosen.append(r)
        if len(chosen) >= top:
            break

    n_valid = sum(1 for r in chosen if r["verdict"] == "valid")
    print(f"candidates available : {len(rows)}  "
          f"({sum(1 for r in rows if r['verdict'] == 'valid')} confirmed valid)")
    print(f"selected             : {len(chosen)}  ({n_valid} confirmed valid)\n")
    print(f"{'#':>3}  {'verdict':8} {'batch/stage':12} {'sector':10} {'email':36} company")
    for i, r in enumerate(chosen, 1):
        tag = r["batch"] or r["fs"] or "-"
        print(f"{i:3}  {r['verdict']:8} {tag:12} {(r['sector'] or '-'):10} "
              f"{r['email'][:36]:36} {(r['cname'] or '')[:26]}")

    if not apply:
        print("\n[REPORT ONLY] rerun with --apply to raise priority")
        return

    backup = f"{DB}.backup-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(DB, backup)
    con.executemany(
        "UPDATE contacts SET priority = ? WHERE id = ?",
        [(PROMOTED_PRIORITY, r["id"]) for r in chosen],
    )
    con.commit()
    print(f"\nbackup: {os.path.basename(backup)}")
    print(f"[APPLIED] {len(chosen)} contacts promoted to priority {PROMOTED_PRIORITY}")


if __name__ == "__main__":
    main()
