#!/usr/bin/env python3
"""probe_batch.py -- verify a small batch of addresses, slowly, then exit.

Written after probing ~15,000 addresses in a few hours moved this host's
Spamhaus listing from PBL (127.0.0.11, "residential range") to CSS (127.0.0.3,
"this host is sending spam"). At volume a stream of RCPT TO probes is
mechanically indistinguishable from directory-harvest reconnaissance, and once
the IP is listed the receiving servers answer `5.7.1 ... blocked` instead of a
real verdict. The probing then produces nothing but "unknown".

So this is deliberately slow and deliberately finite:

  * a global rate limit in probes per minute, not just a worker count
  * one in-flight probe per domain, with a gap between consecutive ones
  * a batch size, after which the process exits rather than continuing

Run it from cron every few hours. Each run picks up where the last stopped,
because progress lives in the database rather than in memory:

  pending_recheck -> 80 verified / retired / left held
  list            -> 80 verified / retired / left held
  guessed         -> only after the two above are exhausted

Commits every 25 addresses, so a run killed halfway keeps its work -- the
earlier whole-file version committed once at the end and lost 1,200 probes when
it was stopped.

Usage:
  python3 probe_batch.py --size 250            # report only
  python3 probe_batch.py --size 250 --apply
  python3 probe_batch.py --size 250 --apply --rate 40
"""
import argparse
import os
import socket
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sqlite3  # noqa: E402

DB = os.path.join(HERE, "turso-full.db")

# Tiers worth spending probes on, best value first. `guessed` is last because
# those are pattern-generated and most will be catch-all or dead anyway.
TIER_ORDER = ["pending_recheck", "list", "guessed"]

VERIFIED_CONF = 80          # must match score_confidence.py TIERS
HELD_CONF = 70


class RateLimiter:
    """A global ceiling on probes per minute, shared across workers.

    Worker count alone does not bound the rate: 4 workers against fast servers
    still issue hundreds of probes a minute. This is the control that actually
    keeps the host off a blocklist."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(per_minute, 1)
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self.interval


class DomainSpacer:
    """Never two probes to one domain closer together than `gap` seconds."""

    def __init__(self, gap: float = 3.0):
        self.gap = gap
        self._locks = defaultdict(threading.Lock)
        self._last = {}

    def wait(self, domain: str) -> None:
        with self._locks[domain]:
            last = self._last.get(domain)
            if last is not None:
                delay = self.gap - (time.monotonic() - last)
                if delay > 0:
                    time.sleep(delay)
            self._last[domain] = time.monotonic()


def ensure_schema(con) -> None:
    """`probe_checked_at` records that a probe happened, whatever it concluded.

    Selecting on `email_verified = 0` instead would re-probe every catch-all and
    every blocked address on every run, forever: those never set the flag, so
    they would monopolise each batch and the queue would never advance."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(contacts)")}
    if "probe_checked_at" not in cols:
        con.execute("ALTER TABLE contacts ADD COLUMN probe_checked_at DATETIME")
        con.execute("CREATE INDEX IF NOT EXISTS idx_contacts_probe_checked "
                    "ON contacts (probe_checked_at)")
        con.commit()


def select_batch(con, size: int) -> list:
    """Addresses to probe, best tier first, oldest first inside a tier."""
    out = []
    for tier in TIER_ORDER:
        if len(out) >= size:
            break
        rows = con.execute(
            """SELECT id, email, email_provenance FROM contacts
               WHERE is_invalid = 0 AND email IS NOT NULL AND email != ''
                 AND email_provenance = ?
                 AND probe_checked_at IS NULL
               ORDER BY id LIMIT ?""",
            (tier, size - len(out))).fetchall()
        out.extend(rows)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, default=250, help="addresses this run")
    ap.add_argument("--rate", type=int, default=40, help="probes per minute, global")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between probes per domain")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    from email_verify import use_ipv4_for_smtp, verify_email
    use_ipv4_for_smtp()
    socket.setdefaulttimeout(20)

    con = sqlite3.connect(args.db, timeout=300)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=300000")

    ensure_schema(con)
    batch = select_batch(con, args.size)
    if not batch:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] nothing left to probe")
        return 0

    remaining = con.execute(
        f"""SELECT COUNT(*) FROM contacts WHERE is_invalid=0 AND email IS NOT NULL
            AND email_provenance IN ({','.join('?'*len(TIER_ORDER))})
            AND probe_checked_at IS NULL""",
        TIER_ORDER).fetchone()[0]

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] probing {len(batch)} "
          f"({remaining} still unverified overall)")
    print(f"  rate {args.rate}/min, {args.workers} workers, {args.gap}s per-domain gap")

    limiter, spacer = RateLimiter(args.rate), DomainSpacer(args.gap)
    tally = Counter()
    t0 = time.time()
    done = 0

    def probe(row):
        nonlocal done
        addr = row["email"]
        limiter.wait()
        spacer.wait(addr.split("@", 1)[1].lower())
        try:
            res = verify_email(addr)
        except Exception:
            res = (None, "error")
        ok, reason = res if isinstance(res, tuple) else (res, "smtp")
        return row, ok, reason or "smtp"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row, ok, reason in pool.map(probe, batch):
            tally[reason if ok is None else ("dead" if ok is False else "alive")] += 1
            if args.apply:
                if ok is False:
                    con.execute(
                        "UPDATE contacts SET is_invalid=1, invalid_reason=?, "
                        "probe_checked_at=? WHERE id=?",
                        (reason[:32], now, row["id"]))
                elif ok is True:
                    con.execute(
                        "UPDATE contacts SET email_verified=1, email_confidence=?, "
                        "email_provenance='verified_guess', confidence_scored_at=?, "
                        "probe_checked_at=? WHERE id=?",
                        (VERIFIED_CONF, now, now, row["id"]))
                else:
                    # Unknown is a real answer and is recorded as one. The row
                    # stays valid and stays held, but leaves the queue -- a
                    # catch-all domain will not become answerable by asking again.
                    con.execute(
                        "UPDATE contacts SET email_confidence=?, invalid_reason=?, "
                        "probe_checked_at=? WHERE id=?",
                        (HELD_CONF, reason[:32], now, row["id"]))
            done += 1
            if done % 25 == 0 and args.apply:
                con.commit()
    if args.apply:
        con.commit()

    el = time.time() - t0
    print(f"  done in {el/60:.1f}m  ({done/max(el/60,0.01):.0f}/min actual)")
    for k, v in tally.most_common():
        print(f"    {k:<26}{v}")
    if not args.apply:
        print("\n  dry run, nothing written")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
