#!/usr/bin/env python3
"""verify_guesses.py -- check the pattern-generated addresses and retire the dead ones.

`scraper.py:infer_email()` produced 6,026 addresses of the shape
`firstname@domain` by construction rather than by finding them. This asks, per
address, whether it can be shown to be wrong -- and removes only the ones that
can be.

The distinction that makes this safe is that there are three answers, not two:

    True   mailbox confirmed              keep, and raise its confidence
    False  mailbox or domain rejected     retire it
    None   cannot be determined           hold: do not send, do not delete

`None` is the majority case here and collapsing it into either neighbour is
expensive in both directions. Calling it True mails a guess and earns a bounce;
calling it False throws away a real lead permanently. Refusing to answer is the
correct behaviour when the probe genuinely cannot tell.

Checks run cheapest-and-most-definitive first, and stop as soon as one settles
the address:

  1. syntax          malformed local part or domain      -> False
  2. known bounce    a hard bounce already recorded      -> False
  3. MX / A record   domain cannot receive mail at all   -> False
  4. SMTP RCPT TO    only where it means anything        -> True / False / None

Step 4 is off by default. Two reasons, both specific to this setup: the sending
IP is on a blocklist, so probes from here get refused in ways that look like
rejections; and 5,320 of these addresses sit on Google-hosted domains, where
the gateway accepts RCPT TO for every address and rejects only at delivery. A
"valid" from those is worth nothing -- that is what put 9 of 15 sends into
bounces on 2026-08-02. Pass --smtp when running from a clean host.

Nothing is deleted. Retired rows get `is_invalid = 1` with a reason, which is
reversible via restore_quarantined.py. Deleting outright is what cost 834
contacts once already.

Usage:
  python3 verify_guesses.py                      # report only
  python3 verify_guesses.py --apply
  python3 verify_guesses.py --apply --smtp       # from a clean IP only
  python3 verify_guesses.py --provenance list    # check a different tier
"""
import argparse
import concurrent.futures as futures
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime

import dns.resolver

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "turso-full.db")

EMAIL_OK = re.compile(r"^[\w.+\-]+@[\w\-]+(\.[\w\-]+)*\.[a-zA-Z]{2,}$")

RESOLVER = dns.resolver.Resolver()
RESOLVER.timeout = 5
RESOLVER.lifetime = 8


def hard_bounced() -> set[str]:
    """Addresses with a recorded hard bounce. The strongest evidence available:
    a real delivery attempt was made and the receiving server refused it."""
    path = os.path.join(HERE, "sent_log_failed.json")
    if not os.path.exists(path):
        return set()
    try:
        rows = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return set()
    out = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        addr, err = (r.get("email") or "").lower(), (r.get("error") or "").lower()
        # Soft failures (full mailbox, rate limit, greylisting) are not evidence
        # the address is wrong, so only unambiguous rejections count here.
        if addr and ("invalid" in err or "not exist" in err or "no such" in err
                     or "unknown" in err or "rejected" in err):
            out.add(addr)
    return out


def domain_accepts_mail(domain: str) -> bool | None:
    """True if the domain publishes MX (or an A record to fall back on).

    False means nothing can ever be delivered there, which is definitive and
    independent of the sending IP -- unlike an SMTP probe. None means the lookup
    itself failed (timeout, servfail) and the domain is not judged either way."""
    try:
        answers = RESOLVER.resolve(domain, "MX")
        if len(answers) > 0:
            return True
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        pass
    except dns.resolver.NXDOMAIN:
        return False                       # domain does not exist at all
    except Exception:
        return None                        # timeout / servfail: unknown
    # No MX is not fatal on its own; RFC 5321 allows falling back to the A record.
    try:
        RESOLVER.resolve(domain, "A")
        return True
    except dns.resolver.NXDOMAIN:
        return False
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False
    except Exception:
        return None


def check_domains(domains: list[str], workers: int) -> dict[str, bool | None]:
    results: dict[str, bool | None] = {}
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (d, ok) in enumerate(
                zip(domains, pool.map(domain_accepts_mail, domains)), 1):
            results[d] = ok
            if i % 250 == 0:
                dead = sum(1 for v in results.values() if v is False)
                print(f"  dns {i:>5}/{len(domains)}  dead={dead}", flush=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--provenance", default="guessed",
                    help="which tier to check (default: guessed)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--smtp", action="store_true",
                    help="also probe mailboxes over SMTP; only from a clean IP")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{args.db}.backup-verify-{stamp}"
        shutil.copy2(args.db, backup)
        print(f"backup: {os.path.basename(backup)}\n")

    con = sqlite3.connect(args.db, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")

    sql = """SELECT id, email, name, company_id FROM contacts
             WHERE email_provenance = ? AND is_invalid = 0 AND email IS NOT NULL"""
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = con.execute(sql, (args.provenance,)).fetchall()
    if not rows:
        print(f"no live contacts with provenance '{args.provenance}'")
        return 0

    bounced = hard_bounced()
    verdicts: dict[int, tuple[bool | None, str]] = {}
    domains_needed = set()

    # 1 + 2: settle what can be settled without a single network call.
    for r in rows:
        addr = r["email"].lower()
        if not EMAIL_OK.match(addr):
            verdicts[r["id"]] = (False, "malformed")
        elif addr in bounced:
            verdicts[r["id"]] = (False, "bounce")
        else:
            domains_needed.add(addr.split("@", 1)[1])

    print(f"{len(rows)} addresses with provenance '{args.provenance}'")
    print(f"  settled offline : {len(verdicts)}")
    print(f"  domains to check: {len(domains_needed)}\n")

    # 3: DNS. Definitive when it says no, and independent of our own IP.
    dns_result = check_domains(sorted(domains_needed), args.workers)

    for r in rows:
        if r["id"] in verdicts:
            continue
        d = r["email"].lower().split("@", 1)[1]
        ok = dns_result.get(d)
        if ok is False:
            verdicts[r["id"]] = (False, "no_mx")
        elif ok is None:
            verdicts[r["id"]] = (None, "dns_inconclusive")
        else:
            verdicts[r["id"]] = (None, "undetermined")

    # 4: SMTP, only if explicitly asked for and only where it can mean anything.
    if args.smtp:
        from email_verify import verify_email, use_ipv4_for_smtp  # noqa: PLC0415
        # Only this host's IPv6 address is blocklisted; the IPv4 one gets clean
        # verdicts. Without this every probe returns None for a reason that has
        # nothing to do with the address being checked.
        use_ipv4_for_smtp()
        pending = [r for r in rows if verdicts[r["id"]][0] is None
                   and verdicts[r["id"]][1] == "undetermined"]
        print(f"\nsmtp probing {len(pending)} addresses "
              f"(catch-all and Google-hosted will still come back unknown)")
        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            for i, (r, res) in enumerate(
                    zip(pending, pool.map(lambda x: verify_email(x["email"]), pending)), 1):
                ok, reason = res if isinstance(res, tuple) else (res, "smtp")
                verdicts[r["id"]] = (ok, reason or "smtp")
                if i % 100 == 0:
                    print(f"  smtp {i:>5}/{len(pending)}", flush=True)

    tally = Counter(reason for _, reason in verdicts.values())
    remove = [cid for cid, (ok, _) in verdicts.items() if ok is False]
    keep = [cid for cid, (ok, _) in verdicts.items() if ok is True]
    hold = [cid for cid, (ok, _) in verdicts.items() if ok is None]

    print(f"\n{'verdict':<12}{'count':>8}   reasons")
    print("-" * 66)
    print(f"{'remove':<12}{len(remove):>8}   "
          + ", ".join(f"{k}={v}" for k, v in tally.items()
                      if k in ("malformed", "bounce", "no_mx", "smtp", "invalid")))
    print(f"{'keep':<12}{len(keep):>8}   confirmed by probe")
    print(f"{'hold':<12}{len(hold):>8}   "
          + ", ".join(f"{k}={v}" for k, v in tally.items()
                      if k in ("undetermined", "dns_inconclusive",
                               "catchall_unverifiable", "smtp_inconclusive",
                               "catchall_inconclusive")))
    print("-" * 66)
    print(f"{'total':<12}{len(verdicts):>8}")

    if not args.smtp:
        print("\nnote: SMTP probing was not run, so 'hold' is everything DNS could")
        print("not rule out. Those stay at confidence 15 and are already gated")
        print("out of sending. Re-run with --smtp from a clean IP to split them.")

    if not args.apply:
        print("\ndry run, nothing written. re-run with --apply")
        con.close()
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.executemany(
        "UPDATE contacts SET is_invalid=1, invalid_reason=? WHERE id=?",
        [(verdicts[cid][1][:32], cid) for cid in remove])
    # 80: above the send gate, below the published tiers. The receiving server
    # confirmed the mailbox exists on a domain proven not to be catch-all, so it
    # will not bounce -- which is the thing being optimised. It stays under
    # 'published' because the address was still constructed: the mailbox is
    # real, but nothing here proves it belongs to the person the row names.
    # Must match TIERS['verified_guess'] in score_confidence.py.
    con.executemany(
        "UPDATE contacts SET email_confidence=80, email_provenance='verified_guess',"
        " confidence_scored_at=?, email_verified=1 WHERE id=?",
        [(now, cid) for cid in keep])
    con.commit()

    print(f"\nretired {len(remove)} addresses (is_invalid=1, reversible)")
    print(f"upgraded {len(keep)} to confidence 70 'verified_guess'")
    live = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0 "
                       "AND email IS NOT NULL").fetchone()[0]
    pool = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0 "
                       "AND email IS NOT NULL AND COALESCE(email_confidence,0)>=75"
                       ).fetchone()[0]
    print(f"live addresses: {live}   sendable (>=75): {pool}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
