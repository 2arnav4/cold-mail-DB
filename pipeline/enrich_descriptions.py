#!/usr/bin/env python3
"""enrich_descriptions.py — fill companies.description for companies that have
nothing to classify on, by reading their homepage <title> and meta description.

Most of the highest-priority contacts belong to companies scraped with no
industry and no description, so classify_companies.py has nothing to work with
and they fall back to the generic template. This grabs the cheapest real signal
available: what the company says about itself in its own <head>.

Only touches companies that are missing a description AND have unsent contacts,
highest priority first, so a short run improves exactly the rows about to be
emailed.

Usage:
  python3 enrich_descriptions.py --limit 50          # report
  python3 enrich_descriptions.py --limit 50 --apply  # fetch and write
"""
import concurrent.futures as futures
import json
import os
import sqlite3
import sys

import requests
from bs4 import BeautifulSoup

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
DB = os.path.join(HERE, "turso-full.db")
TIMEOUT = 8
WORKERS = 8
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def sent_emails() -> set:
    path = os.path.join(HERE, "sent_log.json")
    if not os.path.exists(path):
        return set()
    return {e.lower() for e in json.load(open(path)).get("sent", [])}


def fetch_blurb(domain: str) -> str | None:
    """Return a short self-description from the homepage <head>, or None.

    Tries https first, then http. A homepage that redirects, 403s, or has no
    usable metadata returns None -- that is a normal outcome, not an error, and
    the company simply stays on the generic template.
    """
    for scheme in ("https://", "http://"):
        try:
            r = requests.get(scheme + domain, timeout=TIMEOUT,
                             headers={"User-Agent": UA}, allow_redirects=True)
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            parts = []
            for attrs in ({"name": "description"}, {"property": "og:description"}):
                tag = soup.find("meta", attrs=attrs)
                if tag and tag.get("content"):
                    parts.append(tag["content"].strip())
                    break
            if soup.title and soup.title.string:
                parts.append(soup.title.string.strip())

            blurb = " | ".join(dict.fromkeys(p for p in parts if p))[:300]
            return blurb or None
        except Exception:
            continue
    return None


def main():
    apply = "--apply" in sys.argv
    limit = 50
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    already = sent_emails()

    rows = con.execute("""
        SELECT co.id, co.domain, co.name, MAX(ct.priority) AS pri,
               GROUP_CONCAT(ct.email) AS emails
        FROM companies co JOIN contacts ct ON ct.company_id = co.id
        WHERE (co.description IS NULL OR TRIM(co.description) = '')
          AND (co.industry IS NULL OR TRIM(co.industry) = '')
          AND co.domain IS NOT NULL AND TRIM(co.domain) <> ''
          AND ct.is_invalid = 0
        GROUP BY co.id
        ORDER BY pri DESC, co.id ASC
    """).fetchall()

    # Keep only companies that still have at least one unsent contact.
    pending = []
    for r in rows:
        emails = [e.lower() for e in (r["emails"] or "").split(",") if e]
        if any(e not in already for e in emails):
            pending.append(r)
        if len(pending) >= limit:
            break

    print(f"companies needing a description (with unsent contacts): {len(pending)}")
    if not apply:
        for r in pending[:10]:
            print(f"  {r['pri']}  {r['name'][:28]:28} {r['domain']}")
        print("\n[REPORT ONLY] rerun with --apply to fetch and write")
        return

    found = 0
    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = {pool.submit(fetch_blurb, r["domain"]): r for r in pending}
        for fut in futures.as_completed(jobs):
            r = jobs[fut]
            blurb = fut.result()
            if blurb:
                con.execute("UPDATE companies SET description = ? WHERE id = ?",
                            (blurb, r["id"]))
                found += 1
                print(f"  OK    {r['name'][:24]:24} {blurb[:60]}")
            else:
                print(f"  none  {r['name'][:24]:24} ({r['domain']})")
    con.commit()
    print(f"\n[APPLIED] {found}/{len(pending)} companies enriched")


if __name__ == "__main__":
    main()
