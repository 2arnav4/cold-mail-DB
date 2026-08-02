#!/usr/bin/env python3
"""
pipeline.py — end-to-end demo: company name -> candidate leadership names
(LLM, training knowledge only) -> published contact emails on the company's
own site -> pattern-guess + verify for anyone still missing an email.

Usage:
  python pipeline.py --company "Acme Inc" --domain acme.com
  python pipeline.py --input companies.csv --output results.csv
    (companies.csv needs columns: company,domain)

This is a transparency tool, not a growth-hacking one: every row in the
output records *how* (or whether) an email was found, specifically so the
weak points are visible rather than hidden behind a clean-looking result.
"""
import argparse
import csv
import sys

from email_finder import find_email
from llm_lookup import find_people
from site_scraper import scrape_company_site


def run_for_company(company: str, domain: str) -> list[dict]:
    rows = []

    print(f"\n=== {company} ({domain}) ===")

    print("[1/3] Asking LLM for known leadership names (training knowledge only)...")
    try:
        people = find_people(company)
    except Exception as e:
        print(f"  [pipeline] LLM lookup failed: {e}")
        people = []
    if not people:
        print("  -> no confident names returned")

    print("[2/3] Scraping company's own site for published contacts...")
    try:
        published = scrape_company_site(domain)
    except Exception as e:
        print(f"  [pipeline] site scrape failed: {e}")
        published = []
    print(f"  -> found {len(published)} published address(es) "
          f"({sum(1 for p in published if p['is_generic'])} generic)")

    published_emails = {p["email"].lower() for p in published}

    for person in people:
        name, title, confidence = person["name"], person["title"], person["confidence"]
        match = next(
            (p for p in published
             if name.split()[0].lower() in (p.get("name_hint") or "").lower()
             or name.split()[-1].lower() in (p.get("name_hint") or "").lower()),
            None,
        )

        if match:
            rows.append({
                "company": company, "name": name, "title": title,
                "llm_confidence": confidence, "email": match["email"],
                "source": f"published:{match['source_url']}",
            })
            continue

        print(f"[3/3] No published match for {name!r} — trying pattern-guess + verify...")
        result = find_email(name, domain)
        rows.append({
            "company": company, "name": name, "title": title,
            "llm_confidence": confidence,
            "email": result["email"] or "NOT_FOUND",
            "source": (f"pattern_verified:{result['checked_by']}"
                       if result["email"] else
                       f"unresolved (tried {result['candidates_tried']} patterns)"),
        })

    # Any published, non-generic addresses the LLM step didn't already surface.
    matched_emails = {r["email"].lower() for r in rows if r["email"] != "NOT_FOUND"}
    for p in published:
        if p["is_generic"] or p["email"].lower() in matched_emails:
            continue
        rows.append({
            "company": company, "name": p.get("name_hint") or "(unknown)",
            "title": "", "llm_confidence": "n/a", "email": p["email"],
            "source": f"published:{p['source_url']}",
        })

    if not rows:
        rows.append({
            "company": company, "name": "", "title": "", "llm_confidence": "",
            "email": "NOT_FOUND", "source": "no leads from any stage",
        })

    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--company", help="Single company name")
    ap.add_argument("--domain", help="Single company domain (e.g. acme.com)")
    ap.add_argument("--input", help="CSV with columns: company,domain")
    ap.add_argument("--output", default="results.csv", help="Output CSV path")
    args = ap.parse_args()

    jobs = []
    if args.company and args.domain:
        jobs.append((args.company, args.domain))
    elif args.input:
        with open(args.input, newline="") as f:
            for row in csv.DictReader(f):
                jobs.append((row["company"], row["domain"]))
    else:
        print("Provide --company/--domain or --input companies.csv", file=sys.stderr)
        sys.exit(1)

    all_rows = []
    for company, domain in jobs:
        all_rows.extend(run_for_company(company, domain))

    fieldnames = ["company", "name", "title", "llm_confidence", "email", "source"]
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    resolved = sum(1 for r in all_rows if r["email"] != "NOT_FOUND")
    print(f"\nDone. {resolved}/{len(all_rows)} rows resolved to an email. "
          f"Written to {args.output}")


if __name__ == "__main__":
    main()
