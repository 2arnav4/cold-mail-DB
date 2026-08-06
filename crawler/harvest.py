#!/usr/bin/env python3
"""harvest.py -- read published email addresses off company websites, in bulk.

This is the half of the pipeline that produces addresses. `discover.py` finds
companies and the people at them; nothing there writes an email, on purpose.
Here we go to each company's own site and read what it has chosen to publish.

Why async and not just a bigger thread pool: the work is ~99% waiting on remote
servers. `find_real_emails.py` does the same job synchronously at ~6 workers and
covered 276 of 5,766 domains. At 40 concurrent hosts this clears several
thousand domains an hour, which is what makes an overnight run worth starting.

Politeness is per-host, not global: many hosts in flight at once, but only one
request at a time to any single host, with a delay between them, and robots.txt
consulted once per host and cached. A crawler that hammers one origin gets the
IP blocked, and this machine's IP is already on a blocklist.

Confidence written back, matching score_confidence.py:

  100  mailto: link       the company published it as a clickable address
   95  address in text    published, but parsed out of prose

Both beat the 15 that a `firstname@domain` guess now carries, which is the
entire point of the exercise.

Scope is deliberately narrow, inherited from exec_finder/site_scraper.py:
the company's own domain only, a fixed set of paths plus links discovered on
the homepage, no search engines and no third-party directories.

Usage:
  python3 -m crawler.harvest --limit 50                  # report only
  python3 -m crawler.harvest --limit 500 --apply
  python3 -m crawler.harvest --apply --hiring-first --concurrency 40
  python3 -m crawler.harvest --apply --rescrape          # revisit done domains
"""
import argparse
import asyncio
import os
import re
import sys
import time
import urllib.robotparser
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawler.db import connect, ensure_schema, upsert_contact, utc_now  # noqa: E402
# The acceptance rules already exist and are load-bearing -- importing beats
# maintaining a second copy that can drift out of agreement with the sender.
from find_real_emails import (  # noqa: E402
    acceptable, clean_name, guess_role, local_rank, normalise,
)

UA = "Mozilla/5.0 (compatible; cold-mail-db-harvester/1.0; +personal job search)"

# Ordered by how often they carry a real address. The crawl stops early once a
# personal (non-shared) address is found, so cheap wins should come first.
CANDIDATE_PATHS = [
    "/", "/contact", "/contact-us", "/about", "/about-us", "/team",
    "/our-team", "/company", "/careers", "/jobs", "/leadership", "/people",
    "/press", "/imprint", "/legal/imprint",
]

# Homepage links whose text or href suggests a page worth one extra hop.
LINK_HINTS = re.compile(
    r"contact|about|team|career|job|hiring|people|leadership|imprint|impressum",
    re.I)

EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")

MAX_PAGES_PER_DOMAIN = 8
MAX_BYTES = 1_500_000
PER_HOST_DELAY = 1.2


class HostLimiter:
    """One in-flight request per host, with a delay between consecutive ones.

    Concurrency is bounded globally by a semaphore; this bounds it *per origin*
    so that 40 workers never means 40 simultaneous hits on one small site."""

    def __init__(self, delay: float = PER_HOST_DELAY):
        self.delay = delay
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = {}

    async def __call__(self, host: str):
        return self._locks[host]

    async def wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = self.delay - (time.monotonic() - last)
            if gap > 0:
                await asyncio.sleep(gap)
        self._last[host] = time.monotonic()


class RobotsCache:
    """robots.txt per host, fetched once. Unreadable robots means allowed --
    the same default `urllib.robotparser` takes for a missing file."""

    def __init__(self):
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def allows(self, session, base: str, url: str) -> bool:
        host = urlparse(base).netloc
        async with self._locks[host]:
            if host not in self._cache:
                rp = urllib.robotparser.RobotFileParser()
                try:
                    async with session.get(f"{base}/robots.txt",
                                           timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            rp.parse((await r.text(errors="ignore")).splitlines())
                        else:
                            rp = None
                except Exception:
                    rp = None
                self._cache[host] = rp
        rp = self._cache[host]
        if rp is None:
            return True
        try:
            return rp.can_fetch(UA, url)
        except Exception:
            return True


def decode_cfemail(hexstr: str) -> str | None:
    """Decode Cloudflare's email obfuscation.

    Cloudflare rewrites `foo@bar.com` on a protected page into
    `<a data-cfemail="a1b2c3...">[email&#160;protected]</a>` and restores it with
    JavaScript. The encoding is XOR with a one-byte key that is the first byte
    of the hex string itself, so it needs no browser to undo -- and skipping it
    silently loses every address on a Cloudflare-protected contact page.
    zerodha.com alone carries 26 of them."""
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(raw) < 2:
        return None
    key = raw[0]
    out = "".join(chr(b ^ key) for b in raw[1:])
    return out if "@" in out and "." in out else None


def extract_emails(html: str, url: str) -> list[dict]:
    """Emails on one page, mailto: links first and marked as stronger evidence."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()

    # Cloudflare-protected addresses. Treated as mailto-strength: the company
    # published it as a link and Cloudflare rewrote it, so the evidence that it
    # was a real published address is identical.
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        addr = decode_cfemail(tag.get("data-cfemail", ""))
        if not addr:
            continue
        addr = normalise(addr)
        if not addr or addr in seen:
            continue
        seen.add(addr)
        parent = tag.find_parent()
        hint = (parent.get_text(" ", strip=True)[:60] if parent else "")
        out.append({"email": addr, "hint": hint, "how": "cfemail",
                    "confidence": 100, "source_url": url})

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href.lower().startswith("mailto:"):
            continue
        addr = normalise(href.split(":", 1)[1].split("?")[0])
        if not addr or addr in seen:
            continue
        seen.add(addr)
        parent = tag.find_parent()
        hint = tag.get_text(strip=True) or (
            parent.get_text(" ", strip=True)[:60] if parent else "")
        out.append({"email": addr, "hint": hint, "how": "mailto",
                    "confidence": 100, "source_url": url})

    text = soup.get_text(" ", strip=True)
    for m in EMAIL_RE.finditer(text):
        addr = normalise(m.group(0))
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append({"email": addr, "hint": text[max(0, m.start() - 60):m.start()].strip()[-60:],
                    "how": "text", "confidence": 95, "source_url": url})
    return out


def discover_links(html: str, base: str) -> list[str]:
    """Same-origin links from the homepage that look like contact/team pages."""
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base).netloc
    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        url = urljoin(base, href)
        p = urlparse(url)
        if p.netloc != host or p.scheme not in ("http", "https"):
            continue
        clean = f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
        if clean in seen or len(clean) > 200:
            continue
        if LINK_HINTS.search(href) or LINK_HINTS.search(a.get_text(" ", strip=True)[:60]):
            seen.add(clean)
            found.append(clean)
    return found[:6]


async def fetch(session, url: str, limiter: HostLimiter) -> str | None:
    host = urlparse(url).netloc
    lock = await limiter(host)
    async with lock:
        await limiter.wait(host)
        try:
            async with session.get(url, allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    return None
                ctype = r.headers.get("content-type", "")
                if "html" not in ctype and "text" not in ctype:
                    return None
                return (await r.content.read(MAX_BYTES)).decode(
                    r.charset or "utf-8", errors="ignore")
        except Exception:
            return None


async def harvest_domain(session, domain: str, limiter: HostLimiter,
                         robots: RobotsCache) -> list[dict]:
    """Every acceptable published address for one company. Empty list is normal.

    Stops as soon as a personal-looking address is found: those rank 0 and are
    strictly better than any shared mailbox, so further pages cannot improve on
    it and the request budget is better spent on the next company."""
    base = f"https://{domain}"
    found: dict[str, dict] = {}
    pages, extra = 0, []

    for path in CANDIDATE_PATHS:
        if pages >= MAX_PAGES_PER_DOMAIN:
            break
        url = urljoin(base, path)
        if not await robots.allows(session, base, url):
            continue
        html = await fetch(session, url, limiter)
        pages += 1
        if not html:
            # A dead homepage means a dead domain; the other paths will not
            # resolve either, so stop rather than spend seven more timeouts.
            if path == "/":
                break
            continue

        if path == "/" and not extra:
            extra = discover_links(html, base)

        for item in extract_emails(html, url):
            ok, _ = acceptable(item["email"], domain)
            if not ok:
                continue
            prev = found.get(item["email"])
            if not prev or item["confidence"] > prev["confidence"]:
                found[item["email"]] = item

        if any(local_rank(e) == 0 for e in found):
            break

    for url in extra:
        if pages >= MAX_PAGES_PER_DOMAIN or any(local_rank(e) == 0 for e in found):
            break
        if not await robots.allows(session, base, url):
            continue
        html = await fetch(session, url, limiter)
        pages += 1
        if not html:
            continue
        for item in extract_emails(html, url):
            ok, _ = acceptable(item["email"], domain)
            if not ok:
                continue
            prev = found.get(item["email"])
            if not prev or item["confidence"] > prev["confidence"]:
                found[item["email"]] = item

    # One address per company: a personal address beats any shared mailbox, and
    # careers@ beats sales@. Mailing two people at a five-person startup reads
    # as a mailmerge, which is the impression this whole pipeline avoids.
    ranked = sorted(found.values(),
                    key=lambda i: (local_rank(i["email"]), -i["confidence"]))
    return ranked


def select_targets(con, args) -> list:
    """Companies worth visiting, best prospects first."""
    clauses = ["co.domain IS NOT NULL", "co.domain != ''"]
    if not args.rescrape:
        clauses.append("co.site_scraped_at IS NULL")
    if args.hiring_first:
        clauses.append("COALESCE(co.open_roles,0) > 0")
    if not args.include_covered:
        # Skip companies that already have a well-evidenced address.
        clauses.append("""NOT EXISTS (
            SELECT 1 FROM contacts ct WHERE ct.company_id = co.id
              AND ct.is_invalid = 0 AND ct.email IS NOT NULL
              AND COALESCE(ct.email_confidence,0) >= 75)""")
    sql = f"""SELECT co.id, co.domain, co.name, COALESCE(co.open_roles,0) AS open_roles
              FROM companies co
              WHERE {' AND '.join(clauses)}
              ORDER BY COALESCE(co.open_roles,0) DESC, co.id DESC"""
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    return con.execute(sql).fetchall()


async def run(targets, args, con, write) -> dict:
    """Crawl every target, writing each result as it lands.

    Results are committed incrementally rather than accumulated and written at
    the end. The first version of this held everything in memory until the last
    domain finished, which meant an interruption three hours into an overnight
    run threw away three hours of work -- and `site_scraped_at` was never set,
    so a rerun repeated every request."""
    limiter, robots = HostLimiter(args.delay), RobotsCache()
    sem = asyncio.Semaphore(args.concurrency)
    counts = {"done": 0, "found": 0, "inserted": 0, "updated": 0}
    t0 = time.time()

    connector = aiohttp.TCPConnector(limit=args.concurrency * 2, ttl_dns_cache=600,
                                     ssl=False)
    async with aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}) as session:

        async def one(row):
            async with sem:
                try:
                    hits = await harvest_domain(session, row["domain"], limiter, robots)
                except Exception:
                    hits = []
                write(row, hits, counts)
                counts["done"] += 1
                d = counts["done"]
                if d % 25 == 0:
                    if args.apply:
                        con.commit()
                    rate = d / max(time.time() - t0, 1)
                    eta = (len(targets) - d) / max(rate, 0.01)
                    print(f"  {d:>6}/{len(targets)}  found={counts['found']} "
                          f"({100*counts['found']/d:.0f}%)  "
                          f"new={counts['inserted']}  {rate:.1f}/s  "
                          f"eta {eta/60:.0f}m", flush=True)

        await asyncio.gather(*(one(r) for r in targets))
    if args.apply:
        con.commit()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--apply", action="store_true",
                    help="write findings; without it nothing is changed")
    ap.add_argument("--concurrency", type=int, default=30,
                    help="hosts in flight at once")
    ap.add_argument("--delay", type=float, default=PER_HOST_DELAY,
                    help="seconds between consecutive requests to one host")
    ap.add_argument("--hiring-first", action="store_true",
                    help="only companies with a known open role")
    ap.add_argument("--include-covered", action="store_true",
                    help="also revisit companies that already have a good address")
    ap.add_argument("--rescrape", action="store_true",
                    help="revisit domains crawled on an earlier run")
    ap.add_argument("--max-per-company", type=int, default=1,
                    help="how many addresses to keep per company")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    con = connect(args.db) if args.db else connect()
    ensure_schema(con)

    targets = select_targets(con, args)
    if not targets:
        print("nothing to do — no companies match the filters")
        return 0
    print(f"{len(targets)} companies to visit, {args.concurrency} at a time, "
          f"{args.delay}s between hits on the same host\n")

    samples: list[str] = []

    def write(row, hits, counts):
        """Persist one company's result. Runs inline on the event loop; SQLite
        writes here are sub-millisecond against WAL, so the crawl does not
        stall waiting on them."""
        if args.apply:
            con.execute("UPDATE companies SET site_scraped_at = ? WHERE id = ?",
                        (utc_now(), row["id"]))
        if not hits:
            return
        counts["found"] += 1
        if len(samples) < 12:
            samples.append(hits[0]["email"])
        if not args.apply:
            return
        for item in hits[:args.max_per_company]:
            r = upsert_contact(con, row["id"], {
                "email": item["email"],
                "name": clean_name(item["hint"], item["email"]),
                "role": guess_role(item["hint"], item["email"]),
                "confidence": item["confidence"],
                "provenance": "published",
                "source": "site-harvest",
                "source_url": item["source_url"],
                "priority": 3 if row["open_roles"] else 1,
            })
            counts["inserted"] += r == "inserted"
            counts["updated"] += r == "updated"

    t0 = time.time()
    counts = asyncio.run(run(targets, args, con, write))

    elapsed = time.time() - t0
    print(f"\nvisited {counts['done']} domains in {elapsed/60:.1f}m "
          f"({counts['done']/max(elapsed,1):.1f}/s)")
    print(f"  domains with a published address: {counts['found']} "
          f"({100*counts['found']/max(counts['done'],1):.0f}%)")
    if args.apply:
        print(f"  contacts inserted: {counts['inserted']}")
        print(f"  contacts upgraded: {counts['updated']}")
        pool = con.execute(
            "SELECT COUNT(*) FROM contacts WHERE is_invalid=0 AND email IS NOT NULL "
            "AND COALESCE(email_confidence,0) >= 75").fetchone()[0]
        print(f"  sendable pool now (confidence >= 75): {pool}")
    else:
        print("  sample:", ", ".join(samples) if samples else "(none)")
        print("\ndry run, nothing written. re-run with --apply")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
