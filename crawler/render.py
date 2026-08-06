#!/usr/bin/env python3
"""render.py -- second pass with a real browser, for pages the static fetch cannot read.

`harvest.py` fetches HTML with aiohttp and parses it. That misses a company
whose contact details are assembled by JavaScript after load: a React contact
page, an address written by a script tag to defeat scrapers, a widget that
fetches its content from an API. For those, the bytes aiohttp receives genuinely
do not contain the address, so no amount of better parsing recovers it.

Chrome does read them, at roughly 3-6 seconds per page against ~0.5 for a raw
fetch. That cost is only worth paying where the cheap path already failed, so
this runs strictly as a second pass over domains `harvest.py` visited and came
up empty on.

It does NOT search Google for addresses. That was considered and rejected: a
search engine indexes pages, not mailboxes, so it cannot tell a live mailbox
from a dead one, and for a pattern-generated address there is nothing indexed
to find in the first place. It would also hit automated-query blocking within a
few hundred requests. Rendering the company's own site is the version of "use a
browser" that actually returns addresses.

Measure before committing to a long run: --sample renders a small batch and
reports the recovery rate, so the decision to spend hours on this is made on a
number rather than a hope.

Usage:
  python3 -m crawler.render --sample 25          # measure recovery rate
  python3 -m crawler.render --limit 500 --apply --hiring-first
  python3 -m crawler.render --apply --headed     # watch it work
"""
import argparse
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawler.db import connect, ensure_schema, upsert_contact  # noqa: E402
from crawler.harvest import extract_emails  # noqa: E402
from find_real_emails import acceptable, clean_name, guess_role, local_rank  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Only the pages most likely to carry an address. Every extra path costs seconds
# here, not milliseconds, so the list is much shorter than harvest.py's.
RENDER_PATHS = ["/contact", "/contact-us", "/about", "/team", "/"]

BLOCK_RE = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|woff2?|ttf|mp4|webm)$", re.I)


def render_domain(page, domain: str, timeout_ms: int = 20_000) -> list[dict]:
    """Visit a few pages of one site with JS enabled and pull addresses out."""
    base = f"https://{domain}"
    found: dict[str, dict] = {}

    for path in RENDER_PATHS:
        url = base + path if path != "/" else base
        try:
            resp = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except Exception:
            if path == "/":
                break          # dead site; the rest will not resolve either
            continue
        if not resp or resp.status >= 400:
            continue
        try:
            # Give client-side rendering a moment to attach content, but do not
            # wait for networkidle -- analytics beacons keep many sites busy
            # indefinitely and that turns a 3s page into a 30s timeout.
            page.wait_for_timeout(1200)
            html = page.content()
        except Exception:
            continue

        for item in extract_emails(html, url):
            ok, _ = acceptable(item["email"], domain)
            if not ok:
                continue
            prev = found.get(item["email"])
            if not prev or item["confidence"] > prev["confidence"]:
                found[item["email"]] = item

        if any(local_rank(e) == 0 for e in found):
            break

    return sorted(found.values(),
                  key=lambda i: (local_rank(i["email"]), -i["confidence"]))


def select_targets(con, args) -> list:
    """Domains harvest.py already visited and found nothing on."""
    clauses = [
        "co.domain IS NOT NULL", "co.domain != ''",
        "co.site_scraped_at IS NOT NULL",
        """NOT EXISTS (SELECT 1 FROM contacts ct WHERE ct.company_id = co.id
             AND ct.is_invalid = 0 AND ct.email IS NOT NULL
             AND COALESCE(ct.email_confidence,0) >= 75)""",
    ]
    if args.hiring_first:
        clauses.append("COALESCE(co.open_roles,0) > 0")
    sql = f"""SELECT co.id, co.domain, co.name, COALESCE(co.open_roles,0) open_roles
              FROM companies co WHERE {' AND '.join(clauses)}
              ORDER BY COALESCE(co.open_roles,0) DESC, co.id DESC"""
    n = args.sample or args.limit
    if n:
        sql += f" LIMIT {int(n)}"
    return con.execute(sql).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=None,
                    help="render this many and report the recovery rate, write nothing")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--hiring-first", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    con = connect(args.db) if args.db else connect()
    ensure_schema(con)
    targets = select_targets(con, args)
    if not targets:
        print("nothing to do — every visited domain already has an address")
        return 0

    mode = "SAMPLE" if args.sample else ("APPLY" if args.apply else "DRY RUN")
    print(f"[{mode}] rendering {len(targets)} domains that the static pass came up empty on\n")

    n_found = n_ins = 0
    samples: list[str] = []
    t0 = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900},
                                  java_script_enabled=True)
        ctx.set_default_timeout(20_000)
        page = ctx.new_page()
        page.route(BLOCK_RE, lambda route: route.abort())

        for i, row in enumerate(targets, 1):
            try:
                hits = render_domain(page, row["domain"])
            except Exception:
                hits = []
            if hits:
                n_found += 1
                if len(samples) < 15:
                    samples.append(f"{hits[0]['email']} ({row['domain']})")
                if args.apply and not args.sample:
                    for item in hits[:1]:
                        # A browser run costs seconds per page and hours per
                        # pass. Losing all of it to one lock timeout, as
                        # happened at domain 1,053, is far worse than pausing:
                        # retry rather than let the exception end the run.
                        r = None
                        for attempt in range(5):
                            try:
                                r = upsert_contact(con, row["id"], {
                                    "email": item["email"],
                                    "name": clean_name(item["hint"], item["email"]),
                                    "role": guess_role(item["hint"], item["email"]),
                                    "confidence": item["confidence"],
                                    "provenance": "published",
                                    "source": "chrome-render",
                                    "source_url": item["source_url"],
                                    "priority": 3 if row["open_roles"] else 1,
                                })
                                break
                            except sqlite3.OperationalError as e:
                                if "locked" not in str(e).lower() or attempt == 4:
                                    print(f"  write failed for {row['domain']}: {e}")
                                    break
                                time.sleep(15 * (attempt + 1))
                        n_ins += r == "inserted"
            if i % 10 == 0:
                if args.apply and not args.sample:
                    con.commit()
                rate = i / max(time.time() - t0, 1)
                print(f"  {i:>5}/{len(targets)}  recovered={n_found} "
                      f"({100*n_found/i:.0f}%)  {rate:.2f}/s  "
                      f"eta {(len(targets)-i)/max(rate,0.001)/60:.0f}m", flush=True)

        ctx.close()
        browser.close()

    if args.apply and not args.sample:
        con.commit()

    elapsed = time.time() - t0
    print(f"\nrendered {len(targets)} domains in {elapsed/60:.1f}m "
          f"({len(targets)/max(elapsed,1):.2f}/s)")
    print(f"  recovered an address on: {n_found} ({100*n_found/len(targets):.0f}%)")
    if samples:
        print("  samples:")
        for s in samples[:10]:
            print(f"    {s}")
    if args.apply and not args.sample:
        print(f"  contacts inserted: {n_ins}")

    if args.sample:
        remaining = con.execute("""SELECT COUNT(*) FROM companies co
            WHERE co.site_scraped_at IS NOT NULL AND co.domain IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM contacts ct WHERE ct.company_id=co.id
                AND ct.is_invalid=0 AND ct.email IS NOT NULL
                AND COALESCE(ct.email_confidence,0)>=75)""").fetchone()[0]
        rate = n_found / len(targets)
        per = elapsed / len(targets)
        print(f"\n  empty domains remaining : {remaining}")
        print(f"  projected recovery      : {int(remaining*rate)} addresses")
        print(f"  projected runtime       : {remaining*per/3600:.1f} hours")
        print("\n  sample only, nothing written.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
