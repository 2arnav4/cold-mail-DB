#!/usr/bin/env python3
"""restore_quarantined.py — put back contacts that were quarantined on evidence
that turned out to be about us rather than about them.

Background: verify_smtp mapped every 550/551/553/554 to False, and
move_to_wrong_address() DELETEs from contacts on False. Our sending IP is on
Spamhaus PBL and XBL, so '550 5.7.1 Client host blocked using Spamhaus' -- a
statement about our connection -- deleted live contacts.

Feed this the CSV from the Go verifier (verifier -out). Anything it did NOT
confirm invalid gets restored:

  valid    -> restore (confirmed deliverable, should never have been removed)
  unknown  -> restore (never proven bad; the send path re-verifies live and
              holds it back if still unknown, so this is safe)
  invalid  -> leave in wrong_address (5.1.x, a genuinely dead mailbox)

wrong_address does not carry priority, email_confidence, linkedin_url or notes,
so those are lost -- the row was deleted, not archived in full. Restored rows
get --priority (default 2000, the bottom of the existing range) so they queue
behind the curated priority list rather than displacing it.

Usage:
  python3 restore_quarantined.py results.csv
  python3 restore_quarantined.py results.csv --apply [--priority 2000]
"""
import csv
import os
import shutil
import sqlite3
import sys
from datetime import datetime

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
DB = os.path.join(HERE, "turso-full.db")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    csv_path = args[0]
    apply = "--apply" in sys.argv
    priority = 2000
    if "--priority" in sys.argv:
        priority = int(sys.argv[sys.argv.index("--priority") + 1])

    verdicts = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            verdicts[row["email"].strip().lower()] = row["verdict"].strip().lower()

    recoverable = {e for e, v in verdicts.items() if v in ("valid", "unknown")}
    confirmed_bad = {e for e, v in verdicts.items() if v == "invalid"}
    print(f"from {csv_path}: {len(verdicts)} checked | "
          f"{len(recoverable)} recoverable | {len(confirmed_bad)} confirmed invalid")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    rows = con.execute("""
        SELECT w.id AS wa_id, w.original_contact_id, w.company_id, w.name, w.role, w.email
        FROM wrong_address w
        WHERE w.failed_service = 'smtp'
    """).fetchall()

    live_companies = {r[0] for r in con.execute("SELECT id FROM companies")}
    existing_emails = {
        (r[0] or "").lower() for r in con.execute("SELECT email FROM contacts")
    }

    to_restore, skipped_dupe, skipped_nocompany = [], 0, 0
    for r in rows:
        email = (r["email"] or "").strip()
        if email.lower() not in recoverable:
            continue
        if email.lower() in existing_emails:
            skipped_dupe += 1          # already back in contacts somehow
            continue
        if r["company_id"] not in live_companies:
            skipped_nocompany += 1     # company row is gone; FK would fail
            continue
        to_restore.append(r)

    print(f"  to restore          : {len(to_restore)}")
    print(f"  skipped, already in contacts : {skipped_dupe}")
    print(f"  skipped, company row gone    : {skipped_nocompany}")

    if not apply:
        for r in to_restore[:8]:
            print(f"    {r['email'][:38]:38} {(r['name'] or '')[:22]:22} {(r['role'] or '')[:24]}")
        print("\n[REPORT ONLY] rerun with --apply to restore")
        return

    backup = f"{DB}.backup-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(DB, backup)
    print(f"\nbackup: {os.path.basename(backup)}")

    now = datetime.now().isoformat()
    con.executemany(
        """INSERT OR IGNORE INTO contacts
           (company_id, name, role, email, email_verified, is_invalid, created_at, source, priority)
           VALUES (?, ?, ?, ?, 0, 0, ?, 'restored_from_quarantine', ?)""",
        [(r["company_id"], r["name"], r["role"], r["email"], now, priority)
         for r in to_restore],
    )
    con.executemany(
        "DELETE FROM wrong_address WHERE id = ?",
        [(r["wa_id"],) for r in to_restore],
    )
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0").fetchone()[0]
    left = con.execute("SELECT COUNT(*) FROM wrong_address").fetchone()[0]
    print(f"[APPLIED] restored {len(to_restore)} at priority {priority}")
    print(f"  active contacts now : {total}")
    print(f"  wrong_address now   : {left}")


if __name__ == "__main__":
    main()
