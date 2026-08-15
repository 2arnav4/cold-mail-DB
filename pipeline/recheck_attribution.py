#!/usr/bin/env python3
"""recheck_attribution.py -- re-test every stored name/address pairing.

Why this exists: `clean_name()` in find_real_emails.py decides whether the text
sitting near an address is a person by *excluding* the words that are not names.
It carries a 32-word NOT_A_PERSON list, and its docstring records that the list
was added after 131 rows called "Contact Us" reached the database.

Twenty more got through anyway, because the list does not contain "sign",
"privacy", "learn", "report" or "acquire":

    Sign In                  arjun@example.com
    Privacy Terms            info@example.ai
    Learn More               partnerships@example.com
    Report Abuse             support@example.dev
    Contact Investor Rel...  ir@example.com

Filtering by exclusion does not converge. A company website produces an
unbounded supply of two-capitalised-word phrases, and every word added to the
list only moves the next false positive one page further along.

So this inverts the test. **A personal mailbox carries some of its owner's
name** -- `tkess@` for Todd Kesselman, `nick@` for Nicholas McCormick,
`vsingh@` for Vikram Singh. A navigation label sitting beside `info@` shares
nothing with it. The address becomes the evidence for the name, and unlike a
word list, that is bounded.

It also catches something no crawler fix can reach: rows from imported contact
lists where the name and the address are simply different people.

    Varsha Raghav            tyson@example.dev
    Zachary Gittelman        jamie@example.com
    Mikalai Melchanka        sriram@example.com

Those read as the best rows in the database -- a full name, a real address, a
curated source -- and they greet a stranger by somebody else's name.

Clears names, never addresses. `send_emails.py` renders the greeting as
`(contact_name or "there").split()[0]`, so a cleared row stays perfectly
mailable and simply becomes "Hi there," -- which is what it was always entitled
to.

Usage:
  python3 -m pipeline.recheck_attribution                 # report only
  python3 -m pipeline.recheck_attribution --apply
  python3 -m pipeline.recheck_attribution --apply --db turso-full.db
"""
import argparse
import csv
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# This script lives one directory below the repo root, but the database and the
# shared modules sit at the root. Resolve it explicitly rather than relying on
# the working directory, so the script behaves the same from cron, from an
# editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
DEFAULT_DB = os.path.join(HERE, "turso-full.db")

# The rule itself lives in pipeline/attribution.py, because the writer
# (find_real_emails.clean_name) has to apply exactly the same test before a
# name is stored. Two copies of an acceptance rule drift, and silently.
from pipeline.attribution import (  # noqa: E402
    INVISIBLE, corroborates, is_label,
)


def ensure_schema(con) -> list[str]:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(contacts)")}
    added = []
    if "attribution_checked_at" not in cols:
        con.execute("ALTER TABLE contacts ADD COLUMN attribution_checked_at DATETIME")
        added.append("attribution_checked_at")
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="clear the names that fail; without it nothing is changed")
    ap.add_argument("--include-invalid", action="store_true",
                    help="also examine rows already marked is_invalid")
    ap.add_argument("--no-exempt-researched", action="store_true",
                    help="also test hand-researched rows (they are exempt by default)")
    ap.add_argument("--limit-samples", type=int, default=25)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}", file=sys.stderr)
        return 1

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # Qualified with the alias throughout: `companies` has its own name column,
    # so a bare `name` here is ambiguous once the join is in place.
    where = ["c.email IS NOT NULL", "TRIM(COALESCE(c.email, '')) != ''",
             "c.name IS NOT NULL"]
    if not args.include_invalid:
        where.append("c.is_invalid = 0")
    rows = con.execute(f"""
        SELECT c.id, c.name, c.role, c.email, c.source, c.email_provenance,
               COALESCE(c.email_confidence, 0) AS conf, co.name AS company
          FROM contacts c
          LEFT JOIN companies co ON co.id = c.company_id
         WHERE {' AND '.join(where)}
    """).fetchall()

    # Hand-researched rows are exempt.
    #
    # This rule polices attributions a machine inferred from a page or inherited
    # from a list. A row someone found by hand for one specific contact was
    # paired by a person reading the page, which beats any heuristic -- and
    # those pairings are exactly the ones that look wrong to this test, because
    # a company often gives a named person a departmental address.
    exempt_researched = not args.no_exempt_researched

    doomed, exempt, blank = [], 0, 0
    for r in rows:
        # Invisible characters removed before the emptiness test, or a name made
        # only of zero-width spaces reads as present.
        name = (r["name"] or "").translate(INVISIBLE).strip()
        if not name:
            # Whitespace-only. Not merely useless: send_emails.py renders the
            # greeting as `(name or "there").split()[0]`, and a truthy string
            # that splits to nothing raises IndexError mid-send.
            blank += 1
            doomed.append(r)
            continue
        if exempt_researched and (r["email_provenance"] or "") == "researched":
            exempt += 1
            continue
        # Vocabulary first: a label can corroborate its own mailbox, so the
        # corroboration test cannot be the thing that catches it.
        if is_label(name) or not corroborates(name, r["email"]):
            doomed.append(r)

    print(f"{len(rows)} named addresses examined"
          f"{f' ({exempt} hand-researched rows exempt)' if exempt else ''}")
    print(f"  {len(rows) - len(doomed):>6} corroborated by the address itself")
    print(f"  {len(doomed):>6} not corroborated — the name will be cleared")
    if blank:
        print(f"  {blank:>6} of those are blank names that would crash the greeting\n")
    else:
        print()

    by_prov: dict[str, int] = {}
    for d in doomed:
        k = d["email_provenance"] or "(none)"
        by_prov[k] = by_prov.get(k, 0) + 1
    for k, v in sorted(by_prov.items(), key=lambda x: -x[1]):
        print(f"  {v:>5}  {k}")

    sendable = [d for d in doomed if d["conf"] >= 75]
    print(f"\n{len(sendable)} of them sit at send confidence (>= 75) — "
          f"those would have been greeted by the wrong name.\n")
    print("sample of what goes:")
    for d in doomed[:args.limit_samples]:
        print(f"  {str(d['name'])[:26]:<28} {str(d['email'])[:40]:<42} "
              f"{d['conf']:>3}  {d['email_provenance'] or ''}")

    if not args.apply:
        print("\ndry run, nothing written. re-run with --apply")
        con.close()
        return 0

    if not doomed:
        print("\nnothing to clear")
        con.close()
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{args.db}.backup-attribution-{stamp}"
    shutil.copy2(args.db, backup)
    print(f"\nbackup: {os.path.basename(backup)}")

    # Also written out row by row. The database backup makes this recoverable in
    # bulk; the CSV makes it answerable one row at a time, months later, when
    # somebody asks why a contact lost its name.
    report = os.path.join(HERE, "reports", f"attribution-cleared-{stamp}.csv")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["contact_id", "company", "cleared_name", "cleared_role",
                    "email", "confidence", "provenance", "source"])
        for d in doomed:
            w.writerow([d["id"], d["company"], d["name"], d["role"], d["email"],
                        d["conf"], d["email_provenance"], d["source"]])
    print(f"record: {os.path.relpath(report, HERE)}")

    added = ensure_schema(con)
    if added:
        print(f"added columns: {', '.join(added)}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # The role goes with the name. Both were read off the same text, or arrived
    # in the same list row, so whatever made the name wrong makes the title
    # equally unattributable -- we do not know what tyson@ does either.
    con.executemany(
        "UPDATE contacts SET name = NULL, role = NULL, attribution_checked_at = ?"
        " WHERE id = ?",
        [(now, d["id"]) for d in doomed],
    )
    con.commit()

    remaining = con.execute(
        "SELECT COUNT(*) FROM contacts WHERE email IS NOT NULL AND name IS NOT NULL"
        " AND is_invalid = 0").fetchone()[0]
    print(f"\ncleared {len(doomed)} names · {remaining} corroborated named addresses remain")
    print("those rows are still mailable — they greet 'Hi there,' instead")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
