#!/usr/bin/env python3
"""db_report.py -- what is actually in the contact database, and what is sendable.

Written because the headline number ("7,095 contacts") was misleading in both
directions: most of those addresses were invented, and the ones that were real
were invisible behind the same count. Every figure here is broken down by the
evidence behind it, so "how many emails do I have" has an answer that survives
being asked "how do you know".

Usage:
  python3 db_report.py
  python3 db_report.py --db turso-full.db
"""
import argparse
import json
import os
import sys
import sqlite3

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
DEFAULT_DB = os.path.join(HERE, "turso-full.db")
SEND_THRESHOLD = 75


def rule(title: str = "") -> None:
    print(f"\n{title}\n{'-' * 72}" if title else "-" * 72)


def table(rows, headers, widths) -> None:
    print("  " + "".join(f"{h:<{w}}" for h, w in zip(headers, widths)))
    print("  " + "-" * (sum(widths) - 2))
    for r in rows:
        print("  " + "".join(f"{str(c):<{w}}" for c, w in zip(r, widths)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    q = lambda s, *a: con.execute(s, a).fetchall()          # noqa: E731
    one = lambda s, *a: con.execute(s, a).fetchone()[0]     # noqa: E731

    print(f"database: {os.path.basename(args.db)}")

    rule("companies")
    total_co = one("SELECT COUNT(*) FROM companies")
    hiring = one("SELECT COUNT(*) FROM companies WHERE COALESCE(open_roles,0) > 0")
    scraped = one("SELECT COUNT(*) FROM companies WHERE site_scraped_at IS NOT NULL")
    print(f"  total                     {total_co:>7}")
    print(f"  flagged hiring            {hiring:>7}")
    print(f"  site crawled for addresses{scraped:>7}  ({100*scraped/max(total_co,1):.0f}%)")
    print(f"  never crawled             {total_co-scraped:>7}  <- harvest.py targets")

    rule("contacts by evidence")
    rows = q("""SELECT COALESCE(email_provenance,'(unscored)') p,
                       COUNT(*) n,
                       SUM(email IS NOT NULL) with_addr
                FROM contacts WHERE is_invalid = 0
                GROUP BY p ORDER BY n DESC""")
    table([(r["p"], r["n"], r["with_addr"]) for r in rows],
          ["provenance", "contacts", "with address"], [16, 12, 14])

    rule("sendable pool")
    pool = one(f"""SELECT COUNT(*) FROM contacts
                   WHERE is_invalid=0 AND email IS NOT NULL
                     AND COALESCE(email_confidence,0) >= {SEND_THRESHOLD}""")
    companies_covered = one(f"""SELECT COUNT(DISTINCT company_id) FROM contacts
                                WHERE is_invalid=0 AND email IS NOT NULL
                                  AND COALESCE(email_confidence,0) >= {SEND_THRESHOLD}""")
    awaiting = one("SELECT COUNT(*) FROM contacts WHERE email IS NULL AND is_invalid=0")
    print(f"  confidence >= {SEND_THRESHOLD}          {pool:>7}   <- send_emails.py mails these")
    print(f"  distinct companies        {companies_covered:>7}")
    print(f"  people with no address yet{awaiting:>7}")

    sent_path = os.path.join(HERE, "sent_log.json")
    if os.path.exists(sent_path):
        sent = {e.lower() for e in json.load(open(sent_path)).get("sent", [])}
        fresh = q(f"""SELECT LOWER(email) e FROM contacts
                      WHERE is_invalid=0 AND email IS NOT NULL
                        AND COALESCE(email_confidence,0) >= {SEND_THRESHOLD}""")
        never = sum(1 for r in fresh if r["e"] not in sent)
        print(f"  of those, never emailed   {never:>7}")
        daily = json.load(open(sent_path)).get("daily", {})
        if daily:
            recent = sorted(daily.items())[-14:]
            avg = sum(v for _, v in recent if isinstance(v, int)) / max(len(recent), 1)
            if avg:
                print(f"  at {avg:.0f}/day (14-day avg) that is {never/avg:.0f} days of sending")

    rule("sendable pool by hiring signal")
    rows = q(f"""SELECT CASE WHEN COALESCE(co.open_roles,0) > 0
                             THEN 'hiring' ELSE 'no known role' END k,
                        COUNT(*) n
                 FROM contacts ct JOIN companies co ON ct.company_id = co.id
                 WHERE ct.is_invalid=0 AND ct.email IS NOT NULL
                   AND COALESCE(ct.email_confidence,0) >= {SEND_THRESHOLD}
                 GROUP BY k ORDER BY n DESC""")
    table([(r["k"], r["n"]) for r in rows], ["signal", "contacts"], [18, 10])

    rule("sendable pool by source")
    rows = q(f"""SELECT COALESCE(source,'(none)') s, COUNT(*) n
                 FROM contacts WHERE is_invalid=0 AND email IS NOT NULL
                   AND COALESCE(email_confidence,0) >= {SEND_THRESHOLD}
                 GROUP BY s ORDER BY n DESC LIMIT 12""")
    table([(r["s"][:30], r["n"]) for r in rows], ["source", "contacts"], [32, 10])

    rule("held back")
    rows = q("""SELECT COALESCE(invalid_reason,'(none)') r, COUNT(*) n
                FROM contacts WHERE is_invalid=1 GROUP BY r ORDER BY n DESC LIMIT 8""")
    if rows:
        table([(r["r"], r["n"]) for r in rows], ["reason", "contacts"], [26, 10])
    below = one(f"""SELECT COUNT(*) FROM contacts WHERE is_invalid=0
                    AND email IS NOT NULL
                    AND COALESCE(email_confidence,0) < {SEND_THRESHOLD}""")
    print(f"\n  below the confidence gate {below:>7}  <- guesses, not mailed")
    print("  these keep their name/company, so harvest.py can still")
    print("  replace the address with a published one.")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
