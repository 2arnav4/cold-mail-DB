#!/usr/bin/env python3
"""Normalize companies.funding_stage and rescue location data sitting in
companies.industry. Run with --apply to write; default is a report only.
"""
import os
import re
import sqlite3
import sys
from collections import Counter

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "turso-full.db")
APPLY = "--apply" in sys.argv

# Ordered: first pattern that matches wins, so "pre-series a" beats "series a".
STAGE_RULES = [
    (r"pre[\s\-/]*seed", "pre-seed"),
    (r"pre[\s\-]*series\s*a", "pre-seed"),
    (r"idea|early stage|^early$", "pre-seed"),
    (r"seed", "seed"),
    (r"series\s*a", "series-a"),
    (r"series\s*b", "series-b"),
    (r"series\s*[c-h]|series\s*c\+", "series-c-plus"),
    (r"pre[\s\-]*ipo|ipo|public|bse|nse", "public"),
    (r"growth|late[\s\-]*stage|buyout|\bpe\b", "growth"),
    (r"venture|acquired|well-funded|other|unknown", "other"),
]

# A value is a location, not an industry, if it looks like "City, Region".
# Checked as plain logic rather than one regex -- easier to read and to extend
# when a new place shows up in the scraped data.
US_STATES = set("""AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI
MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC""".split())

PLACE_WORDS = {w.lower() for w in """States Kingdom India Netherlands Germany France
Singapore Canada Australia Israel Sweden Spain Brazil Japan China Ireland Poland
Karnataka Maharashtra Delhi Telangana Nadu Haryana England Ontario Jawa Noord
Ile-de-France California Texas Washington Virginia Massachusetts Colorado Illinois
Florida Georgia Oregon Utah Arizona Berlin Bavaria Barat Holland Israel Estonia
Denmark Norway Finland Portugal Belgium Austria Switzerland Mexico Chile Argentina""".split()}


def is_location(value: str) -> bool:
    if "," not in value:
        return False
    tail = value.rsplit(",", 1)[1].strip()
    if tail.upper() in US_STATES:
        return True
    return any(w.strip(".").lower() in PLACE_WORDS for w in tail.split())


def normalize_stage(raw):
    v = (raw or "").strip().lower()
    if not v:
        return None
    for pattern, bucket in STAGE_RULES:
        if re.search(pattern, v):
            return bucket
    return "other"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    cols = {r["name"] for r in con.execute("PRAGMA table_info(companies)")}
    if "location" not in cols:
        if APPLY:
            con.execute("ALTER TABLE companies ADD COLUMN location VARCHAR(255)")
            print("added companies.location")
        else:
            print("would add column: companies.location")

    rows = con.execute(
        "SELECT id, industry, funding_stage FROM companies"
    ).fetchall()

    stage_changes, moved, kept = [], [], []
    for r in rows:
        new_stage = normalize_stage(r["funding_stage"])
        if new_stage != (r["funding_stage"] or None):
            stage_changes.append((r["id"], r["funding_stage"], new_stage))

        ind = (r["industry"] or "").strip()
        if ind and is_location(ind):
            moved.append((r["id"], ind))
        elif ind:
            kept.append(ind)

    print(f"\nfunding_stage: {len(stage_changes)} rows change")
    print("  new buckets:", dict(Counter(n for _, _, n in stage_changes if n).most_common()))
    print(f"\nindustry: {len(moved)} location values move to companies.location")
    print(f"          {len(kept)} real industry values stay")
    print("  top industries kept:", dict(Counter(kept).most_common(8)))
    print("  sample moves:", [m[1] for m in moved[:5]])

    if not APPLY:
        print("\n[REPORT ONLY] rerun with --apply to write")
        return

    con.executemany(
        "UPDATE companies SET funding_stage = ? WHERE id = ?",
        [(new, cid) for cid, _, new in stage_changes],
    )
    con.executemany(
        "UPDATE companies SET location = industry, industry = NULL WHERE id = ?",
        [(cid,) for cid, _ in moved],
    )
    con.commit()
    print(f"\n[APPLIED] {len(stage_changes)} stages normalized, {len(moved)} locations moved")


if __name__ == "__main__":
    main()
