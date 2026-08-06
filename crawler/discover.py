#!/usr/bin/env python3
"""discover.py -- find companies that are hiring, using Chrome where it earns it.

Three stages, and the split between them is the whole design:

  Chrome (Playwright)  ->  the rotating search credential + infinite-scroll boards
  Algolia over HTTP    ->  the 6,124-company directory, ~7 requests
  plain HTTP           ->  per-company founders and open roles

Chrome is used for two things it is genuinely needed for and nothing else:

1. **The search key.** YC's directory is backed by Algolia, and the browser-side
   key is minted into `window.AlgoliaOpts` on each page load. Hardcoding one
   gets a 403 the moment it rotates -- which is exactly what happened to the
   key that was tried first here. Loading the page in a browser and reading the
   value back is the only version of this that keeps working.
2. **Infinite-scroll job boards**, where the server sends the first page and
   JavaScript fetches the rest.

Everything downstream is plain HTTP, because once a company slug is known its
detail page is server-rendered and `requests` reads it ~40x faster than driving
a browser to the same place.

What this deliberately does not do: invent an email address. Founders are
written with `email = NULL` and `harvest.py` resolves real published addresses
afterwards. The database already carries 6,029 `firstname@domain` guesses and
the point of this crawler is to stop adding to that pile. See crawler/db.py.

Usage:
  python3 -m crawler.discover --source directory --hiring-only
  python3 -m crawler.discover --source directory --batches "Summer 2025,Winter 2026"
  python3 -m crawler.discover --source jobs --roles eng
  python3 -m crawler.discover --source directory --with-founders --limit 500
"""
import argparse
import concurrent.futures as futures
import html
import json
import os
import random
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawler.db import (connect, ensure_schema, get_state, set_state,  # noqa: E402
                        upsert_company, upsert_contact, clean_domain)

YC = "https://www.ycombinator.com"
YC_INDEX = "YCCompany_production"
YC_ROLES = ["eng", "design", "product", "operations", "marketing", "sales", "recruiting"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

# Industries worth mailing for a backend/full-stack internship. Everything else
# is still stored -- the filter is applied at send time, not at crawl time --
# but this drives `priority`, which is what the sender orders on.
TECH_INDUSTRIES = {
    "b2b", "engineering, product and design", "developer tools", "infrastructure",
    "analytics", "security", "fintech", "artificial intelligence", "data",
    "productivity", "api", "devops", "cloud",
}


# ── Chrome: the rotating key, and scroll-only boards ─────────────────────────

def _launch(pw, headed: bool):
    browser = pw.chromium.launch(headless=not headed)
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.route(re.compile(r"\.(png|jpg|jpeg|gif|svg|woff2?|ttf)$"),
               lambda route: route.abort())
    return browser, ctx, page


def fetch_algolia_credentials(headed: bool = False) -> tuple[str, str]:
    """Read YC's browser-side Algolia app id and search key out of a live page.

    The key is scoped by YC to the public company indices and expires, so it is
    read fresh on every run rather than stored. Raises if the shape of the page
    changed, because silently falling back to a stale key would show up much
    later as an unexplained 403."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser, ctx, page = _launch(pw, headed)
        try:
            page.goto(f"{YC}/companies", timeout=60_000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            opts = page.evaluate("() => window.AlgoliaOpts || null")
            if not opts:
                # The value lives in an inline <script> before hydration runs;
                # fall back to the raw document if the global is not set yet.
                content = page.content()
                m = re.search(r'window\.AlgoliaOpts\s*=\s*(\{.*?\})\s*;', content, re.S)
                if not m:
                    raise RuntimeError("AlgoliaOpts not found on /companies")
                opts = json.loads(html.unescape(m.group(1)))
        finally:
            ctx.close()
            browser.close()

    app, key = opts.get("app"), opts.get("key")
    if not app or not key:
        raise RuntimeError(f"incomplete AlgoliaOpts: {opts!r}")
    return app, key


def discover_job_board_slugs(roles: list[str], max_scrolls: int,
                             headed: bool) -> set[str]:
    """Scroll YC's per-role job boards and collect company slugs.

    Separate from the directory because these boards are filtered to companies
    with a live posting for that role -- a stronger signal than the directory's
    `isHiring` flag, which a company can leave set indefinitely."""
    from playwright.sync_api import sync_playwright

    slugs: set[str] = set()
    with sync_playwright() as pw:
        browser, ctx, page = _launch(pw, headed)
        for role in roles:
            before_role = len(slugs)
            try:
                page.goto(f"{YC}/jobs/role/{role}", timeout=60_000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            except Exception as e:
                print(f"  [{role}] load failed: {str(e)[:80]}")
                continue

            stagnant = 0
            for i in range(max_scrolls):
                hrefs = page.eval_on_selector_all(
                    'a[href^="/companies/"]',
                    "els => els.map(e => e.getAttribute('href'))")
                before = len(slugs)
                for h in hrefs:
                    m = re.match(r"^/companies/([a-z0-9][a-z0-9\-]*)$", h or "")
                    if m:
                        slugs.add(m.group(1))
                # A "Show N companies" / "Load more" control appears instead of
                # true infinite scroll on some boards; click it when present.
                try:
                    btn = page.query_selector(
                        "button:has-text('Show'), button:has-text('Load more')")
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1500)
                except Exception:
                    pass
                stagnant = stagnant + 1 if len(slugs) == before else 0
                if stagnant >= 3:
                    break
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(random.randint(600, 1100))
            print(f"  [{role}] {len(slugs) - before_role} new, {len(slugs)} total")
        ctx.close()
        browser.close()
    return slugs


# ── Algolia: the whole directory in a handful of requests ────────────────────

def algolia_page(app: str, key: str, page_no: int, hits: int = 1000,
                 filters: str = "") -> dict:
    body = {
        "query": "",
        "hitsPerPage": hits,
        "page": page_no,
        "attributesToRetrieve": [
            "name", "slug", "website", "batch", "industries", "subindustry",
            "all_locations", "team_size", "isHiring", "one_liner", "tags",
            "status", "long_description",
        ],
    }
    if filters:
        body["filters"] = filters
    r = requests.post(
        f"https://{app.lower()}-dsn.algolia.net/1/indexes/{YC_INDEX}/query",
        headers={"x-algolia-api-key": key, "x-algolia-application-id": app,
                 "Content-Type": "application/json"},
        json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def algolia_facets(app: str, key: str, facet: str = "batch") -> dict[str, int]:
    """Every value of a facet and its count, e.g. {'Summer 2025': 166, ...}."""
    r = requests.post(
        f"https://{app.lower()}-dsn.algolia.net/1/indexes/{YC_INDEX}/query",
        headers={"x-algolia-api-key": key, "x-algolia-application-id": app,
                 "Content-Type": "application/json"},
        json={"query": "", "hitsPerPage": 0, "facets": [facet],
              "maxValuesPerFacet": 1000},
        timeout=30)
    r.raise_for_status()
    return r.json().get("facets", {}).get(facet, {})


def iter_directory(app: str, key: str, filters: str = "", max_pages: int = None):
    """Yield every company in the directory.

    Algolia's `paginationLimitedTo` caps a single query at 1,000 results no
    matter what `nbHits` says -- the first version of this reported "6,125
    companies across 1 page" and silently returned a sixth of them. So the
    directory is sliced by batch facet instead: every YC batch is a few hundred
    companies, comfortably under the cap, and the union is the whole index."""
    batches = algolia_facets(app, key, "batch")
    if not batches:
        # No facet data -- fall back to a flat paginate and accept the cap.
        first = algolia_page(app, key, 0, filters=filters)
        print(f"  directory: {first.get('nbHits', 0)} companies, no batch facet")
        yield from first.get("hits", [])
        return

    total = sum(batches.values())
    print(f"  directory: {total} companies across {len(batches)} batches")
    slices = sorted(batches.items(), key=lambda kv: -kv[1])
    if max_pages:
        slices = slices[:max_pages]

    for i, (batch, count) in enumerate(slices, 1):
        flt = f'batch:"{batch}"'
        if filters:
            flt = f"({filters}) AND {flt}"
        page_no, seen = 0, 0
        while True:
            try:
                res = algolia_page(app, key, page_no, hits=1000, filters=flt)
            except requests.RequestException as e:
                print(f"  batch {batch!r} page {page_no} failed: {str(e)[:70]}")
                break
            hits = res.get("hits", [])
            yield from hits
            seen += len(hits)
            page_no += 1
            if page_no >= res.get("nbPages", 0) or not hits:
                break
            time.sleep(0.2)
        if i % 10 == 0:
            print(f"    {i}/{len(slices)} batches, latest {batch!r}: {seen}")
        time.sleep(0.15)


def hit_to_company(hit: dict, source: str) -> dict | None:
    domain = clean_domain(hit.get("website") or "")
    if not domain:
        return None
    industries = hit.get("industries") or []
    tags = hit.get("tags") or []
    is_tech = any(str(x).lower() in TECH_INDUSTRIES for x in industries + tags)
    return {
        "domain": domain,
        "name": hit.get("name") or domain,
        "source": source,
        "industry": ", ".join(industries)[:64],
        "location": (hit.get("all_locations") or "")[:255],
        "batch": (hit.get("batch") or "")[:16],
        "description": (hit.get("one_liner") or "")[:1000],
        "open_roles": 1 if hit.get("isHiring") else 0,
        "careers_url": f"{YC}/companies/{hit.get('slug')}/jobs" if hit.get("isHiring") else "",
        "slug": hit.get("slug"),
        "is_hiring": bool(hit.get("isHiring")),
        "is_tech": is_tech,
        "team_size": hit.get("team_size") or 0,
    }


# ── plain HTTP: per-company founders and real role counts ────────────────────

def parse_yc_page(html_text: str) -> dict | None:
    """Pull the react_on_rails payload out of a YC company page.

    The JSON sits HTML-escaped inside `div[data-page="..."]`. Regex rather than
    a DOM parse because that payload is ~90KB of a ~95KB document, so a full
    parse spends its time on markup nothing here ever reads."""
    m = re.search(
        r'<div[^>]*id="[^"]*ShowPage-react-component[^"]*"[^>]*data-page="([^"]*)"',
        html_text)
    if not m:
        return None
    try:
        return json.loads(html.unescape(m.group(1))).get("props", {})
    except json.JSONDecodeError:
        return None


def fetch_company_detail(slug: str, timeout: int = 20) -> dict | None:
    """Founders and open postings for one slug. None when unreadable."""
    try:
        r = SESSION.get(f"{YC}/companies/{slug}", timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    props = parse_yc_page(r.text)
    if not props:
        return None

    co = props.get("company") or {}
    jobs = props.get("jobPostings") or []
    founders = []
    for f in (co.get("founders") or []):
        full = (f.get("full_name") or "").strip()
        if full:
            founders.append({
                "name": full,
                "role": (f.get("title") or "Founder").strip()[:128],
                "linkedin_url": f.get("linkedin_url") or None,
            })
    return {
        "slug": slug,
        "domain": clean_domain(co.get("website") or ""),
        "name": co.get("name") or slug,
        "founders": founders,
        "open_roles": len(jobs),
        "eng_roles": sum(1 for j in jobs if (j.get("role") or "") == "eng"),
        "intern_roles": sum(1 for j in jobs if (j.get("type") or "") == "Internship"),
    }


def save_founders(con, company_id: int, detail: dict, source: str) -> int:
    added = 0
    # Priority rides the hiring signal so the sender reaches companies with a
    # live engineering role before ones whose board has gone stale.
    priority = 3 if detail["eng_roles"] else (2 if detail["open_roles"] else 1)
    for f in detail["founders"]:
        if upsert_contact(con, company_id, {
            "name": f["name"], "role": f["role"], "email": None,
            "linkedin_url": f["linkedin_url"], "source": source,
            "source_url": f"{YC}/companies/{detail['slug']}",
            "priority": priority,
        }) == "inserted":
            added += 1
    return added


# ── orchestration ────────────────────────────────────────────────────────────

def run_directory(con, args) -> None:
    print("stage 1 — Chrome, reading the rotating Algolia key")
    app, key = fetch_algolia_credentials(args.headed)
    print(f"  app={app} key={key[:12]}…\n")

    filters = []
    if args.hiring_only:
        filters.append("isHiring:true")
    if args.batches:
        wanted = [b.strip() for b in args.batches.split(",") if b.strip()]
        filters.append(" OR ".join(f'batch:"{b}"' for b in wanted))
    flt = " AND ".join(f"({f})" for f in filters)

    print(f"stage 2 — Algolia{' filter: ' + flt if flt else ''}")
    seen, n_co, n_skip = set(), 0, 0
    hiring_slugs = []

    for hit in iter_directory(app, key, filters=flt, max_pages=args.max_pages):
        info = hit_to_company(hit, "yc-directory-crawler")
        if not info or info["domain"] in seen:
            n_skip += 1
            continue
        seen.add(info["domain"])
        cid = upsert_company(con, info)
        if cid:
            n_co += 1
            if info["is_hiring"] and info["slug"]:
                hiring_slugs.append((info["slug"], cid))
        if n_co % 500 == 0 and n_co:
            con.commit()
            print(f"  {n_co} companies written")
    con.commit()
    print(f"stage 2 done — {n_co} companies written, {n_skip} skipped "
          f"(no usable domain or duplicate)\n")

    if not args.with_founders:
        print("founders not fetched (pass --with-founders to add them)")
        return

    todo = hiring_slugs[:args.limit] if args.limit else hiring_slugs
    print(f"stage 3 — HTTP, founders for {len(todo)} hiring companies "
          f"({args.workers} workers)")
    t0, n_f, n_fail = time.time(), 0, 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        details = pool.map(fetch_company_detail, [s for s, _ in todo])
        for i, ((slug, cid), detail) in enumerate(zip(todo, details), 1):
            if not detail:
                n_fail += 1
            else:
                n_f += save_founders(con, cid, detail, "yc-directory-crawler")
                con.execute(
                    "UPDATE companies SET open_roles=? WHERE id=?",
                    (detail["open_roles"], cid))
            if i % 50 == 0:
                con.commit()
                print(f"  {i:>5}/{len(todo)}  founders={n_f} failed={n_fail}  "
                      f"{i/max(time.time()-t0,1):.1f}/s")
    con.commit()
    print(f"stage 3 done — {n_f} founders written, {n_fail} pages unreadable")


def run_jobs(con, args) -> None:
    roles = YC_ROLES if args.roles == "all" else [r.strip() for r in args.roles.split(",")]
    print(f"stage 1 — Chrome, scrolling {len(roles)} job board(s)")
    slugs = sorted(discover_job_board_slugs(roles, args.max_scrolls, args.headed))
    print(f"  {len(slugs)} unique companies\n")

    done = set(json.loads(get_state(con, "yc_jobs_done", "[]"))) if args.resume else set()
    todo = [s for s in slugs if s not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"stage 2 — HTTP, {len(todo)} company pages ({args.workers} workers)")
    t0, n_co, n_f, n_fail = time.time(), 0, 0, 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (slug, detail) in enumerate(
                zip(todo, pool.map(fetch_company_detail, todo)), 1):
            if not detail or not detail["domain"]:
                n_fail += 1
            else:
                cid = upsert_company(con, {
                    "domain": detail["domain"], "name": detail["name"],
                    "source": "yc-jobs-crawler", "open_roles": detail["open_roles"],
                    "careers_url": f"{YC}/companies/{slug}/jobs",
                })
                if cid:
                    n_co += 1
                    n_f += save_founders(con, cid, detail, "yc-jobs-crawler")
            done.add(slug)
            if i % 25 == 0:
                con.commit()
                set_state(con, "yc_jobs_done", json.dumps(sorted(done)))
                print(f"  {i:>5}/{len(todo)}  companies={n_co} founders={n_f} "
                      f"failed={n_fail}  {i/max(time.time()-t0,1):.1f}/s")
    con.commit()
    set_state(con, "yc_jobs_done", json.dumps(sorted(done)))
    print(f"stage 2 done — {n_co} companies, {n_f} founders, {n_fail} failed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["directory", "jobs"], default="directory")
    ap.add_argument("--hiring-only", action="store_true",
                    help="directory: only companies flagged as hiring")
    ap.add_argument("--batches", default=None,
                    help='directory: comma-separated, e.g. "Summer 2025,Winter 2026"')
    ap.add_argument("--with-founders", action="store_true",
                    help="directory: also fetch founder names per hiring company")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="directory: cap Algolia pages (1000 companies each)")
    ap.add_argument("--roles", default="eng", help="jobs: comma-separated, or 'all'")
    ap.add_argument("--max-scrolls", type=int, default=60)
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel detail fetches; above ~8 YC starts throttling")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    con = connect(args.db) if args.db else connect()
    added = ensure_schema(con)
    if added:
        print(f"schema: added {', '.join(added)}\n")

    t0 = time.time()
    try:
        if args.source == "directory":
            run_directory(con, args)
        else:
            run_jobs(con, args)
    finally:
        con.commit()

    total = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    hiring = con.execute("SELECT COUNT(*) FROM companies WHERE open_roles > 0").fetchone()[0]
    people = con.execute(
        "SELECT COUNT(*) FROM contacts WHERE email IS NULL").fetchone()[0]
    print(f"\nelapsed {time.time()-t0:.0f}s")
    print(f"  companies in db      : {total}")
    print(f"  flagged hiring       : {hiring}")
    print(f"  people awaiting an address: {people}  -> run crawler/harvest.py")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
