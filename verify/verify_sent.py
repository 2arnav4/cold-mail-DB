#!/usr/bin/env python3
"""verify_sent.py -- re-probe every address that has already been mailed.

The tracker's dashboard mixes three different kinds of fact: a bounce it
actually received as a DSN, a send it never heard back about, and an "open" that
may be a scanner. None of those tell you whether the mailbox is alive *today*.
1,673 addresses have been mailed and most sit in the middle category, which
looks like success and often is not: a silent non-delivery is indistinguishable
from being ignored.

This probes each one over SMTP, with the catch-all detection that was previously
skipped for Google-hosted domains, and sorts them into what is actually known:

  dead        the server rejects it -- a resend or follow-up will bounce
  alive       accepted, on a domain proven to reject unknown recipients
  unknown     catch-all domain, or the probe could not get a clean answer

Deliberately *not* done: writing these results to the tracker as bounces. The
tracker's bounce count means "a delivery status notification arrived for this
address". A probe saying a mailbox is gone is a different fact, gathered a
different way, possibly months later. Merging them would destroy the only honest
number on the dashboard. Dead addresses are retired locally instead, which is
what stops send_emails.py mailing them again.

Usage:
  python3 verify_sent.py                # report only
  python3 verify_sent.py --apply
  python3 verify_sent.py --apply --limit 200
"""
import argparse
import concurrent.futures as futures
import json
import os
import shutil
import socket
import sqlite3
import sys
from collections import Counter
from datetime import datetime

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DB = os.path.join(HERE, "turso-full.db")


def sent_addresses(con) -> list[str]:
    """Every address we have mailed, from the log and the sent_contacts table."""
    out = set()
    p = os.path.join(HERE, "sent_log.json")
    if os.path.exists(p):
        out |= {str(e).lower().strip() for e in json.load(open(p)).get("sent", []) if e}
    try:
        out |= {r[0].lower() for r in con.execute(
            "SELECT email FROM sent_contacts WHERE email IS NOT NULL")}
    except sqlite3.OperationalError:
        pass
    return sorted(a for a in out if "@" in a)


def known_bounced() -> set[str]:
    """Addresses that already produced a real delivery failure."""
    p = os.path.join(HERE, "sent_log_failed.json")
    if not os.path.exists(p):
        return set()
    try:
        rows = json.load(open(p))
    except (OSError, json.JSONDecodeError):
        return set()
    return {(r.get("email") or "").lower() for r in rows
            if isinstance(r, dict) and r.get("email")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    from email_verify import use_ipv4_for_smtp, verify_email
    use_ipv4_for_smtp()
    socket.setdefaulttimeout(20)

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{args.db}.backup-sentcheck-{stamp}"
        shutil.copy2(args.db, backup)
        print(f"backup: {os.path.basename(backup)}\n")

    con = sqlite3.connect(args.db, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=120000")

    addrs = sent_addresses(con)
    if args.limit:
        addrs = addrs[:args.limit]
    bounced = known_bounced()
    print(f"addresses already mailed : {len(addrs)}")
    print(f"  with a recorded bounce : {len(addrs & bounced) if isinstance(addrs, set) else len([a for a in addrs if a in bounced])}")
    print(f"probing {len(addrs)} with {args.workers} workers\n")

    results: dict[str, tuple] = {}
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (addr, res) in enumerate(zip(addrs, pool.map(verify_email, addrs)), 1):
            ok, reason = res if isinstance(res, tuple) else (res, "smtp")
            results[addr] = (ok, reason or "smtp")
            if i % 100 == 0:
                dead = sum(1 for v in results.values() if v[0] is False)
                print(f"  {i:>5}/{len(addrs)}  dead={dead}", flush=True)

    dead = [a for a, (ok, _) in results.items() if ok is False]
    alive = [a for a, (ok, _) in results.items() if ok is True]
    unknown = [a for a, (ok, _) in results.items() if ok is None]
    tally = Counter(r for _, r in results.values())

    print(f"\n{'verdict':<10}{'count':>8}{'share':>9}   meaning")
    print("-" * 74)
    n = max(len(results), 1)
    print(f"{'dead':<10}{len(dead):>8}{100*len(dead)/n:>8.0f}%   server rejects it; a follow-up would bounce")
    print(f"{'alive':<10}{len(alive):>8}{100*len(alive)/n:>8.0f}%   accepted on a domain that rejects unknowns")
    print(f"{'unknown':<10}{len(unknown):>8}{100*len(unknown)/n:>8.0f}%   catch-all or no clean answer")
    print("-" * 74)
    print("reasons:", ", ".join(f"{k}={v}" for k, v in tally.most_common(6)))

    already = [a for a in dead if a in bounced]
    newly = [a for a in dead if a not in bounced]
    print(f"\nof the dead addresses:")
    print(f"  already known to have bounced : {len(already)}")
    print(f"  NOT previously known bad      : {len(newly)}  <- silently never delivered")
    if newly:
        print("  examples:", ", ".join(newly[:8]))

    if not args.apply:
        print("\ndry run, nothing written. re-run with --apply")
        con.close()
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.executemany(
        "UPDATE contacts SET is_invalid=1, invalid_reason='probe_dead' "
        "WHERE LOWER(email)=? AND is_invalid=0", [(a,) for a in dead])
    con.executemany(
        "UPDATE contacts SET email_verified=1, confidence_scored_at=? "
        "WHERE LOWER(email)=? AND is_invalid=0", [(now, a) for a in alive])
    con.commit()
    print(f"\nretired {con.total_changes} rows locally (reversible)")
    print("tracker bounce counts left untouched -- see the module docstring")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
