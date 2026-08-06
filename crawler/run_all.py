#!/usr/bin/env python3
"""run_all.py -- the whole acquisition pipeline, in order, unattended.

Stages, each of which is resumable on its own:

  1. discover  YC directory -> companies (Chrome for the rotating search key)
  2. hn        Who-is-hiring threads -> companies
  3. harvest   every uncovered company domain -> published addresses
  4. score     recompute confidence over everything

Ordering is not arbitrary. Discovery is cheap and fast (minutes) and decides
how much work stage 3 has; harvest is the long pole (hours) and wants the
company table already full so it makes one pass instead of three.

Stage 3 writes as it goes and records `site_scraped_at` per domain, so killing
this at any point and rerunning it resumes rather than restarting. That is the
property that makes an overnight run safe to walk away from.

Usage:
  python3 -m crawler.run_all --hn-months 24
  python3 -m crawler.run_all --skip discover,hn      # harvest only
  python3 -m crawler.run_all --dry-run               # print the plan
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# -u on every child: their stdout is a pipe, not a tty, so Python block-buffers
# it and a stage that runs for an hour shows nothing until it exits. That makes
# a long run indistinguishable from a hung one.
PY = [sys.executable, "-u"]


def run_stage(name: str, argv: list[str], dry: bool) -> tuple[bool, float]:
    print(f"\n{'='*70}\n  stage: {name}\n  {' '.join(argv)}\n{'='*70}", flush=True)
    if dry:
        return True, 0.0
    t0 = time.time()
    proc = subprocess.run(argv, cwd=HERE)
    elapsed = time.time() - t0
    ok = proc.returncode == 0
    print(f"\n  [{name}] {'ok' if ok else f'FAILED rc={proc.returncode}'} "
          f"in {elapsed/60:.1f}m", flush=True)
    return ok, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hn-months", type=int, default=24)
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--skip", default="",
                    help="comma-separated stage names to skip")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands without running them")
    args = ap.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    started = datetime.now()
    print(f"pipeline started {started:%Y-%m-%d %H:%M:%S}")

    stages = [
        ("discover", [*PY, "-m", "crawler.discover", "--source", "directory",
                      "--with-founders", "--workers", "6"]),
        ("hn", [*PY, "-m", "crawler.hn", "--months", str(args.hn_months), "--apply"]),
        ("harvest", [*PY, "-m", "crawler.harvest", "--apply",
                     "--concurrency", str(args.concurrency),
                     "--delay", str(args.delay)]),
        ("score", [*PY, "score_confidence.py", "--apply"]),
    ]

    results = []
    for name, argv in stages:
        if name in skip:
            print(f"\n  [skip] {name}")
            continue
        ok, elapsed = run_stage(name, argv, args.dry_run)
        results.append((name, ok, elapsed))
        if not ok and name != "hn":
            # A failed HN scrape is survivable -- the other sources still fill
            # the table. A failed discover or harvest means the rest is
            # operating on stale input, so stop rather than paper over it.
            print(f"\naborting: {name} failed and later stages depend on it")
            break

    total = sum(e for _, _, e in results)
    print(f"\n{'='*70}\n  pipeline summary\n{'='*70}")
    for name, ok, elapsed in results:
        print(f"  {name:<12} {'ok' if ok else 'FAILED':<8} {elapsed/60:>6.1f}m")
    print(f"  {'total':<12} {'':<8} {total/60:>6.1f}m")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
