#!/usr/bin/env python3
"""import_local_sources.py -- pull addresses out of the spreadsheets into the database.

`hr-database/` and `data_sources/` hold the lists collected by hand over time.
3,810 unique addresses live in those files and 1,564 of them were never imported
-- `send_emails.py` can read a CSV directly, so a list could be mailed from
without ever reaching `contacts`, which means no bounce retirement, no cooldown,
no dedupe against everything else.

This imports what is missing, matching columns case-insensitively because the
files disagree on headers (`Email`, `email address`, `Work Email`, `Mail`).

Every row lands as provenance `list` at confidence 75. That is deliberate: these
addresses came from somewhere real, but nothing here says where or when, and the
files are months old. Running verify_guesses.py over them afterwards is what
turns "probably real" into "the mail server confirmed it".

Usage:
  python3 import_local_sources.py                 # report only
  python3 import_local_sources.py --apply
  python3 import_local_sources.py --apply --unsent-only
"""
import argparse
import csv
import glob
import json
import os
import re
import sys

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from crawler.db import connect, ensure_schema, upsert_company, upsert_contact, clean_domain  # noqa: E402

EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")

SOURCES = ["hr-database/*.csv", "hr-database/*.xlsx", "data_sources/*.xlsx", "*.xlsx"]

EMAIL_KEYS = ("email", "email address", "e-mail", "mail", "work email",
              "email id", "emailid", "personal email")
NAME_KEYS = ("name", "full name", "contact name", "person", "first name", "hr name")
COMPANY_KEYS = ("company", "company name", "organisation", "organization",
                "startup", "employer")
ROLE_KEYS = ("role", "title", "designation", "position", "job title")
DOMAIN_KEYS = ("domain", "website", "url", "company domain", "site")


def pick(row: dict, keys) -> str:
    for want in keys:
        for k, v in row.items():
            if k and k.strip().lower() == want and v:
                return str(v).strip()
    # Fall back to a substring match: 'Contact Email ID' matches 'email'.
    for want in keys:
        for k, v in row.items():
            if k and want in k.strip().lower() and v:
                return str(v).strip()
    return ""


def rows_from_csv(path):
    with open(path, newline="", encoding="utf-8-sig", errors="ignore") as f:
        yield from csv.DictReader(f)


def rows_from_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        try:
            header = [str(c).strip() if c else "" for c in next(rows)]
        except StopIteration:
            continue
        # A sheet whose first row has no email-ish header is probably headerless;
        # emails are still recoverable by regex below, so do not skip it.
        for r in rows:
            yield {h: v for h, v in zip(header, r) if h}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--unsent-only", action="store_true",
                    help="skip addresses that already appear in sent_log.json")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    con = connect(args.db) if args.db else connect()
    ensure_schema(con)

    known = {r[0].lower() for r in con.execute(
        "SELECT email FROM contacts WHERE email IS NOT NULL")}
    sent = set()
    p = os.path.join(HERE, "sent_log.json")
    if os.path.exists(p):
        sent = {e.lower() for e in json.load(open(p)).get("sent", [])}

    files = []
    for pat in SOURCES:
        files.extend(glob.glob(os.path.join(HERE, pat)))
    files = sorted(set(f for f in files if os.path.isfile(f)))

    candidates: dict[str, dict] = {}
    for path in files:
        reader = rows_from_xlsx if path.endswith((".xlsx", ".xls")) else rows_from_csv
        try:
            for row in reader(path):
                blob = " ".join(str(v) for v in row.values() if v)
                m = EMAIL_RE.search(pick(row, EMAIL_KEYS) or blob)
                if not m:
                    continue
                addr = m.group(0).lower().strip(".,;:")
                if addr in known or addr in candidates:
                    continue
                if args.unsent_only and addr in sent:
                    continue
                company = pick(row, COMPANY_KEYS)
                # Falling back to the address's own domain is right for
                # rahul@acme.com and wrong for rahul@gmail.com -- the latter
                # creates one "gmail.com company" that collects every personal
                # address in the file and then looks like a 12-person startup to
                # anything that ranks by company. Those rows are still mailable;
                # they just have no company domain, so they carry the file's
                # company name and are excluded from company-level ranking.
                addr_domain = addr.split("@", 1)[1]
                domain = clean_domain(pick(row, DOMAIN_KEYS))
                if not domain and clean_domain(addr_domain):
                    domain = addr_domain          # clean_domain rejects free-mail
                if not domain:
                    domain = ""
                candidates[addr] = {
                    "email": addr,
                    "name": pick(row, NAME_KEYS)[:255] or None,
                    "role": pick(row, ROLE_KEYS)[:128] or None,
                    "company": company or domain,
                    "domain": domain,
                    "file": os.path.basename(path),
                }
        except Exception as e:
            print(f"  !! {os.path.basename(path)}: {e}")

    by_file: dict[str, int] = {}
    for c in candidates.values():
        by_file[c["file"]] = by_file.get(c["file"], 0) + 1

    print(f"{'file':<50}{'new addresses':>15}")
    print("-" * 66)
    for f, n in sorted(by_file.items(), key=lambda kv: -kv[1]):
        print(f"{f[:48]:<50}{n:>15}")
    print("-" * 66)
    print(f"{'TOTAL':<50}{len(candidates):>15}")
    already_sent = sum(1 for a in candidates if a in sent)
    print(f"\n  of these, already emailed at some point: {already_sent}")
    print(f"  never emailed:                          {len(candidates)-already_sent}")

    if not args.apply:
        print("\ndry run, nothing written. re-run with --apply")
        return 0

    inserted = 0
    for c in candidates.values():
        domain = clean_domain(c["domain"])
        if not domain:
            continue
        cid = upsert_company(con, {
            "domain": domain, "name": c["company"], "source": "local_import",
        })
        if not cid:
            continue
        if upsert_contact(con, cid, {
            "email": c["email"], "name": c["name"], "role": c["role"],
            "confidence": 75, "provenance": "list",
            "source": f"local_import:{c['file'][:24]}",
            "priority": 1,
        }) == "inserted":
            inserted += 1
    con.commit()

    pool = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0 "
                       "AND email IS NOT NULL AND COALESCE(email_confidence,0)>=75"
                       ).fetchone()[0]
    print(f"\ninserted {inserted} contacts")
    print(f"sendable pool now: {pool}")
    print("run: python3 verify_guesses.py --apply --smtp --provenance list")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
