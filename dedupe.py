#!/usr/bin/env python3
"""dedupe.py -- collapse rows that describe the same company or the same person.

Exact duplicate addresses cannot exist: `contacts` carries UNIQUE(email). What
does exist is the same real-world entity spread across several rows, which the
constraint cannot see:

  A. subdomain splits    absinthe.network + guides.absinthe.network
  B. TLD splits          100ms.com + 100ms.live, same name
  C. repeated people     the same person at the same company, twice

Each is merged into whichever row carries the better evidence, and the loser is
retired rather than deleted. Companies are the exception -- an emptied duplicate
company row is deleted, because once its contacts have been moved it describes
nothing and leaving it re-splits the next crawl.

What this deliberately does NOT treat as a duplicate: a company with two
different people at it. Those are two real people, and deleting one loses a
contact. The rule that only one of them should be mailed is a *sending* rule,
enforced in send_emails.py's query, not a reason to destroy data.

Merging direction is always toward the better-evidenced row:
published > researched > list > unattributed > guessed, then higher confidence,
then the row that has been emailed before (its history is worth keeping).

Usage:
  python3 dedupe.py                  # report only
  python3 dedupe.py --apply
  python3 dedupe.py --apply --skip-tld   # subdomains + people only
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "turso-full.db")

# Higher wins. Mirrors score_confidence.py's tiers.
PROV_RANK = {
    "published": 5, "researched": 4, "verified_guess": 3,
    "list": 2, "unattributed": 1, "guessed": 0,
}

# Multi-part public suffixes we must not mistake for "subdomain of .uk".
TWO_LABEL_TLDS = {
    "co.uk", "co.in", "com.au", "co.nz", "com.br", "co.jp", "com.sg",
    "co.za", "com.mx", "org.uk", "ac.uk", "net.au", "com.tr",
}

NAME_NOISE = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|"
                        r"pvt|private|technologies|technology|labs|lab|"
                        r"software|solutions|systems|group|holdings)\b", re.I)


def apex(domain: str) -> str:
    """The registrable domain: 'guides.absinthe.network' -> 'absinthe.network'."""
    parts = domain.lower().strip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in TWO_LABEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_root(domain: str) -> str:
    """The label before the TLD: '100ms.live' -> '100ms'."""
    return apex(domain).split(".")[0]


def norm_name(name: str) -> str:
    """Company name reduced to its distinctive part, for comparison only."""
    n = NAME_NOISE.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", n)


def contact_score(row) -> tuple:
    """Sort key: bigger is better evidence."""
    return (
        PROV_RANK.get(row["email_provenance"] or "", -1),
        row["email_confidence"] or 0,
        1 if row["email"] else 0,
        1 if row["source_url"] else 0,
        -(row["id"] or 0),          # older row wins ties; its history is longer
    )


def company_score(row, contact_counts, sent_domains) -> tuple:
    """Which of two company rows should survive."""
    return (
        1 if row["domain"] in sent_domains else 0,   # never orphan sent history
        1 if len(row["domain"].split(".")) == 2 else 0,   # prefer the apex
        contact_counts.get(row["id"], 0),
        1 if (row["open_roles"] or 0) > 0 else 0,
        -(row["id"] or 0),
    )


def merge_company(con, keep_id: int, drop_id: int, apply: bool) -> int:
    """Move contacts from drop_id to keep_id, then delete the empty company.

    UNIQUE(email) means a contact cannot simply be reassigned when the surviving
    company already holds that address; those are retired instead of moved, so
    the constraint is never fought with an INSERT OR REPLACE that would silently
    drop the better row."""
    moved = 0
    kept_emails = {
        (r[0] or "").lower()
        for r in con.execute("SELECT email FROM contacts WHERE company_id=?", (keep_id,))
    }
    for row in con.execute(
            "SELECT id, email FROM contacts WHERE company_id=?", (drop_id,)).fetchall():
        addr = (row["email"] or "").lower()
        if addr and addr in kept_emails:
            if apply:
                con.execute("UPDATE contacts SET is_invalid=1, invalid_reason='dupe_merged'"
                            " WHERE id=?", (row["id"],))
            continue
        if apply:
            con.execute("UPDATE contacts SET company_id=? WHERE id=?", (keep_id, row["id"]))
        if addr:
            kept_emails.add(addr)
        moved += 1
    if apply:
        con.execute("DELETE FROM companies WHERE id=?", (drop_id,))
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-tld", action="store_true",
                    help="do not merge same-name companies on different TLDs")
    args = ap.parse_args()

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{args.db}.backup-dedupe-{stamp}"
        shutil.copy2(args.db, backup)
        print(f"backup: {os.path.basename(backup)}\n")

    con = sqlite3.connect(args.db, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA foreign_keys=OFF")

    companies = con.execute(
        "SELECT id, domain, name, COALESCE(open_roles,0) open_roles FROM companies "
        "WHERE domain IS NOT NULL AND domain != ''").fetchall()
    counts = {r["company_id"]: r["n"] for r in con.execute(
        "SELECT company_id, COUNT(*) n FROM contacts GROUP BY company_id")}
    sent_domains = {r[0] for r in con.execute(
        "SELECT DISTINCT company_domain FROM send_queue WHERE company_domain IS NOT NULL")}

    by_domain = {c["domain"].lower(): c for c in companies}
    merged_co = moved_ct = 0
    plans: list[tuple] = []

    # A. subdomain splits -- definitive: the apex already exists as its own row.
    for c in companies:
        d = c["domain"].lower()
        a = apex(d)
        if a != d and a in by_domain and by_domain[a]["id"] != c["id"]:
            plans.append(("subdomain", by_domain[a], c))

    # B. TLD splits -- same distinctive name AND same domain root.
    if not args.skip_tld:
        groups: dict[tuple, list] = defaultdict(list)
        planned = {c["id"] for _, _, c in plans}
        for c in companies:
            if c["id"] in planned:
                continue
            key = (norm_name(c["name"]), domain_root(c["domain"]))
            if key[0] and key[1]:
                groups[key].append(c)
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda r: company_score(r, counts, sent_domains), reverse=True)
            for loser in rows[1:]:
                plans.append(("tld", rows[0], loser))

    print(f"company merges planned: {len(plans)}")
    for kind, keep, drop in plans[:8]:
        print(f"  [{kind}] {drop['domain']:<34} -> {keep['domain']}")
    if len(plans) > 8:
        print(f"  … and {len(plans)-8} more")

    seen_drop = set()
    for kind, keep, drop in plans:
        if drop["id"] in seen_drop or drop["id"] == keep["id"]:
            continue
        seen_drop.add(drop["id"])
        moved_ct += merge_company(con, keep["id"], drop["id"], args.apply)
        merged_co += 1

    # C. the same person listed twice at one company.
    dupe_people = con.execute("""
        SELECT company_id, LOWER(name) n, COUNT(*) c
        FROM contacts WHERE name IS NOT NULL AND name != '' AND is_invalid = 0
        GROUP BY company_id, n HAVING c > 1""").fetchall()

    retired_people = 0
    for grp in dupe_people:
        rows = con.execute(
            """SELECT id, email, email_confidence, email_provenance, source_url
               FROM contacts WHERE company_id=? AND LOWER(name)=? AND is_invalid=0""",
            (grp["company_id"], grp["n"])).fetchall()
        rows = sorted(rows, key=contact_score, reverse=True)
        for loser in rows[1:]:
            if args.apply:
                con.execute("UPDATE contacts SET is_invalid=1, invalid_reason='dupe_person'"
                            " WHERE id=?", (loser["id"],))
            retired_people += 1

    print(f"\nduplicate people retired: {retired_people}")

    if args.apply:
        con.commit()

    co_after = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    live = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0 "
                       "AND email IS NOT NULL").fetchone()[0]
    pool = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0 "
                       "AND email IS NOT NULL AND COALESCE(email_confidence,0)>=75"
                       ).fetchone()[0]
    multi = con.execute("""SELECT COUNT(*) FROM (
        SELECT company_id FROM contacts WHERE is_invalid=0 AND email IS NOT NULL
          AND COALESCE(email_confidence,0)>=75
        GROUP BY company_id HAVING COUNT(*)>1)""").fetchone()[0]

    print(f"\ncompanies merged : {merged_co}")
    print(f"contacts moved   : {moved_ct}")
    print(f"companies now    : {co_after}")
    print(f"live addresses   : {live}")
    print(f"sendable (>=75)  : {pool}")
    print(f"\ncompanies still holding >1 sendable address: {multi}")
    print("  those are different people, not duplicates. send_emails.py picks one")
    print("  per company at send time rather than deleting the others.")

    if not args.apply:
        print("\ndry run, nothing written. re-run with --apply")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
