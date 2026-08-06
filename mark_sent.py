#!/usr/bin/env python3
"""mark_sent.py -- record in the database which addresses have already been mailed.

`sent_log.json` is the only record of what has gone out, which means every
question about the *remaining* pool had to be answered by loading a JSON file
and doing set arithmetic in whatever script was asking. Different scripts did it
differently, and `contacts` itself had no idea any of its rows had been used.

This writes it down once, in two places:

  contacts.sent_at    timestamp of the first send to that address, or NULL
  sent_contacts       one row per send, with the company and campaign context

Nothing is deleted or moved out of `contacts`. A sent contact is still a real
contact -- it is needed for cooldowns, for follow-ups, and to stop a later crawl
re-adding it as new. It is simply excluded from "how many are left", which is
the number that actually gets acted on.

Re-runnable: reads sent_log.json each time and fills in anything new.

Usage:
  python3 mark_sent.py            # report only
  python3 mark_sent.py --apply
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "turso-full.db")


def load_sent() -> dict[str, dict]:
    """{address: {sent_at, company}} from sent_log.json."""
    path = os.path.join(HERE, "sent_log.json")
    if not os.path.exists(path):
        return {}
    data = json.load(open(path))
    details = data.get("details", {}) or {}
    out = {}
    for addr in data.get("sent", []) or []:
        a = str(addr).lower().strip()
        if not a:
            continue
        d = details.get(addr) or details.get(a) or {}
        out[a] = {
            "sent_at": d.get("sent_at") or d.get("time") or "",
            "company": d.get("company") or "",
        }
    return out


def ensure_schema(con) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(contacts)")}
    if "sent_at" not in cols:
        con.execute("ALTER TABLE contacts ADD COLUMN sent_at DATETIME")
    con.execute("""CREATE TABLE IF NOT EXISTS sent_contacts (
        email       VARCHAR(255) PRIMARY KEY,
        contact_id  INTEGER,
        company     VARCHAR(255),
        sent_at     DATETIME,
        recorded_at DATETIME NOT NULL
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_contacts_sent_at ON contacts (sent_at)")
    con.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=120000")
    ensure_schema(con)

    sent = load_sent()
    if not sent:
        print("sent_log.json holds no sends")
        return 0

    known = {r["email"].lower(): r["id"] for r in con.execute(
        "SELECT id, email FROM contacts WHERE email IS NOT NULL")}

    matched = {a: known[a] for a in sent if a in known}
    unmatched = [a for a in sent if a not in known]
    already = con.execute("SELECT COUNT(*) FROM contacts WHERE sent_at IS NOT NULL").fetchone()[0]

    print(f"addresses in sent_log.json      : {len(sent)}")
    print(f"  matched to a contact row      : {len(matched)}")
    print(f"  mailed from a CSV, not in db  : {len(unmatched)}")
    print(f"  already marked in the database: {already}")

    live_before = con.execute(
        "SELECT COUNT(*) FROM contacts WHERE is_invalid=0 AND email IS NOT NULL "
        "AND COALESCE(email_confidence,0) >= 75").fetchone()[0]
    print(f"\nsendable pool before excluding sent: {live_before}")
    print(f"sendable pool after  excluding sent: "
          f"{live_before - sum(1 for a in matched if a in sent)}")

    if not args.apply:
        print("\ndry run, nothing written. re-run with --apply")
        return 0

    now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.executemany(
        "UPDATE contacts SET sent_at = COALESCE(sent_at, ?) WHERE id = ?",
        [(sent[a]["sent_at"] or now, cid) for a, cid in matched.items()])
    con.executemany(
        """INSERT INTO sent_contacts (email, contact_id, company, sent_at, recorded_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(email) DO UPDATE SET
               contact_id = COALESCE(excluded.contact_id, contact_id),
               sent_at    = COALESCE(sent_contacts.sent_at, excluded.sent_at)""",
        [(a, matched.get(a), sent[a]["company"], sent[a]["sent_at"] or now, now)
         for a in sent])
    con.commit()

    marked = con.execute("SELECT COUNT(*) FROM contacts WHERE sent_at IS NOT NULL").fetchone()[0]
    total_sent = con.execute("SELECT COUNT(*) FROM sent_contacts").fetchone()[0]
    remaining = con.execute(
        "SELECT COUNT(*) FROM contacts WHERE is_invalid=0 AND email IS NOT NULL "
        "AND COALESCE(email_confidence,0) >= 75 AND sent_at IS NULL").fetchone()[0]

    print(f"\ncontacts marked sent : {marked}")
    print(f"sent_contacts rows   : {total_sent}")
    print(f"sendable and unsent  : {remaining}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
