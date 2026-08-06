#!/usr/bin/env python3
"""hn.py -- companies from Hacker News "Ask HN: Who is hiring?" threads.

YC's directory tops out at ~6,100 companies and skews to YC's own portfolio.
The monthly Who-is-hiring thread is the other large free source of *actively
hiring* tech companies, and it is better signal than a directory flag: a person
sat down and wrote the post, this month, because they need to fill a seat.

There are ~320 such threads with roughly a thousand comments each. Only recent
ones are worth reading -- a 2016 posting tells us nothing about who is hiring
now -- so `--months` bounds how far back to go.

The thread has a strong posting convention:

    Company Name | Job Title | Location | REMOTE | https://company.com/careers

which is what makes extraction tractable. Comments that do not follow it are
mined for a bare URL instead, and comments with neither are dropped rather than
guessed at. Roughly a third of comments yield nothing; that is expected.

No addresses are written here. Companies land in the `companies` table and
`harvest.py` resolves published addresses from their sites afterwards, the same
path YC-sourced companies take.

Usage:
  python3 -m crawler.hn --months 12                # report only
  python3 -m crawler.hn --months 24 --apply
  python3 -m crawler.hn --months 6 --apply --interns-only
"""
import argparse
import html as htmllib
import os
import re
import sys
import time
from collections import Counter

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawler.db import connect, ensure_schema, upsert_company, clean_domain  # noqa: E402

HN_API = "https://hn.algolia.com/api/v1/search"
UA = "Mozilla/5.0 (compatible; cold-mail-db-crawler/1.0)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

# Signals that a posting is worth a student's time. Absence is not fatal --
# the company still goes in the database -- but presence raises priority.
INTERN_RE = re.compile(r"\bintern(ship)?s?\b|\bnew ?grad\b|\bjunior\b|\bentry.level\b", re.I)
REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|\bwfh\b", re.I)
INDIA_RE = re.compile(r"\bindia\b|\bbangalore\b|\bbengaluru\b|\bdelhi\b|\bnoida\b|"
                      r"\bgurgaon\b|\bgurugram\b|\bmumbai\b|\bpune\b|\bhyderabad\b", re.I)
STACK_RE = re.compile(
    r"\b(react|node|typescript|javascript|python|golang|\bgo\b|postgres|mongodb|"
    r"next\.?js|express|docker|kubernetes|rust|django|flask|fastapi)\b", re.I)

# Hosts that appear as the first URL in a comment but are never the employer.
NOT_EMPLOYERS = {
    "news.ycombinator.com", "lever.co", "greenhouse.io", "workable.com",
    "ashbyhq.com", "bamboohr.com", "recruitee.com", "breezy.hr", "jazzhr.com",
    "smartrecruiters.com", "applytojob.com", "workday.com", "myworkdayjobs.com",
    "docs.google.com", "forms.gle", "notion.so", "airtable.com", "typeform.com",
    "indeed.com", "glassdoor.com", "stackoverflow.com", "jobs.lever.co",
}


def strip_html(s: str) -> str:
    return " ".join(TAG_RE.sub(" ", htmllib.unescape(s or "")).split())


def find_threads(months: int) -> list[dict]:
    """The N most recent Who-is-hiring stories, newest first.

    Uses `search_by_date` against the `whoishiring` account rather than a
    relevance search for the title: relevance ranking returned November 2025 as
    the newest thread when August 2026 existed, because it scores text match and
    points, not recency. The account posts these on the 1st of every month, so
    listing its stories chronologically is exact.

    "Who wants to be hired?" is posted by the same account on the same day and
    is the mirror image -- job seekers advertising themselves. Including it
    would fill the database with individuals' personal sites."""
    hits, page = [], 0
    while len(hits) < months * 3 and page < 6:
        r = SESSION.get("https://hn.algolia.com/api/v1/search_by_date", params={
            "tags": "story,author_whoishiring", "hitsPerPage": 50, "page": page},
            timeout=30)
        r.raise_for_status()
        data = r.json()
        for h in data.get("hits", []):
            title = (h.get("title") or "").lower()
            if "who is hiring" in title and "wants to be hired" not in title:
                hits.append(h)
        if page + 1 >= data.get("nbPages", 0):
            break
        page += 1
        time.sleep(0.2)
    hits.sort(key=lambda h: h.get("created_at_i") or 0, reverse=True)
    return hits[:months]


def fetch_comments(story_id: str, max_pages: int = 12) -> list[str]:
    """Top-level comment texts for one thread."""
    out = []
    for page in range(max_pages):
        try:
            r = SESSION.get(HN_API, params={
                "tags": f"comment,story_{story_id}", "hitsPerPage": 100,
                "page": page}, timeout=30)
            r.raise_for_status()
        except requests.RequestException:
            break
        data = r.json()
        for h in data.get("hits", []):
            text = strip_html(h.get("comment_text") or "")
            if len(text) > 40:
                out.append(text)
        if page + 1 >= data.get("nbPages", 0):
            break
        time.sleep(0.2)
    return out


def employer_domain(urls: list[str]) -> str | None:
    """First URL that plausibly belongs to the employer rather than its ATS.

    An `jobs.lever.co/acme` link names the employer in its path, not its host,
    so those hosts are skipped and the next URL tried; a comment whose only
    link is an ATS gets no domain rather than `lever.co`, which would otherwise
    become one company row collecting hundreds of unrelated postings."""
    for u in urls:
        d = clean_domain(u)
        if not d:
            continue
        if d in NOT_EMPLOYERS or any(d.endswith("." + n) for n in NOT_EMPLOYERS):
            continue
        return d
    return None


def parse_comment(text: str) -> dict | None:
    """One posting -> {name, domain, ...}, or None when nothing reliable is there."""
    urls = URL_RE.findall(text)
    domain = employer_domain(urls)
    if not domain:
        return None

    # The convention puts the company first, pipe-delimited. Fall back to the
    # domain's own label rather than inventing a name from prose.
    head = text.split("|")[0].strip() if "|" in text[:200] else ""
    name = head if 1 < len(head) <= 60 and not head.lower().startswith("http") else ""
    if not name:
        name = domain.split(".")[0].replace("-", " ").title()

    return {
        "domain": domain,
        "name": name[:255],
        "description": text[:500],
        "has_intern": bool(INTERN_RE.search(text)),
        "is_remote": bool(REMOTE_RE.search(text)),
        "is_india": bool(INDIA_RE.search(text)),
        "stack_hits": len(set(m.lower() for m in STACK_RE.findall(text))),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=12,
                    help="how many recent threads to read")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--interns-only", action="store_true",
                    help="keep only postings mentioning intern/new-grad/junior")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    con = connect(args.db) if args.db else connect()
    ensure_schema(con)

    threads = find_threads(args.months)
    print(f"{len(threads)} threads:")
    for t in threads[:5]:
        print(f"  {t['objectID']}  {t.get('title')}  ({t.get('num_comments')} comments)")
    if len(threads) > 5:
        print(f"  … and {len(threads)-5} more\n")

    seen: dict[str, dict] = {}
    stats = Counter()
    for i, t in enumerate(threads, 1):
        comments = fetch_comments(t["objectID"])
        n_new = 0
        for c in comments:
            stats["comments"] += 1
            info = parse_comment(c)
            if not info:
                stats["no_domain"] += 1
                continue
            if args.interns_only and not info["has_intern"]:
                stats["not_intern"] += 1
                continue
            if info["domain"] not in seen:
                seen[info["domain"]] = info
                n_new += 1
            stats["parsed"] += 1
        print(f"  [{i}/{len(threads)}] {t.get('title','')[:45]:<45} "
              f"{len(comments):>4} comments  +{n_new} companies  "
              f"({len(seen)} unique)")

    print(f"\ncomments read     : {stats['comments']}")
    print(f"  no usable domain: {stats['no_domain']}")
    if args.interns_only:
        print(f"  no intern signal: {stats['not_intern']}")
    print(f"unique companies  : {len(seen)}")
    print(f"  mentioning intern/newgrad: {sum(1 for v in seen.values() if v['has_intern'])}")
    print(f"  mentioning remote        : {sum(1 for v in seen.values() if v['is_remote'])}")
    print(f"  mentioning India         : {sum(1 for v in seen.values() if v['is_india'])}")

    if not args.apply:
        print("\nsample:", ", ".join(list(seen)[:15]))
        print("dry run, nothing written. re-run with --apply")
        return 0

    n_new = 0
    for domain, info in seen.items():
        cid = upsert_company(con, {
            "domain": domain,
            "name": info["name"],
            "source": "hn-whoishiring",
            "description": info["description"],
            "open_roles": 1,          # the posting itself is the open role
            "careers_url": "",
        })
        if cid:
            n_new += 1
        if n_new % 500 == 0 and n_new:
            con.commit()
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"\nwrote {n_new} companies; {total} in the database")
    print("run crawler/harvest.py to resolve addresses for the new domains")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
