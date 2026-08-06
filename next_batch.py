#!/usr/bin/env python3
"""next_batch.py -- show exactly who the next send would go to, and why.

`send_emails.py --dry-run` renders the full email for every contact it would
mail, which is the wrong shape for deciding whether the *list* is right. This
prints one line per contact with the evidence behind the address and the reason
it ranked where it did, so the batch can be inspected before anything goes out.

Ranking, strongest first:

  1. bounce risk   published+probed > probed > published > list
  2. startup       a YC batch or a VC portfolio source, and not a large company
  3. hiring        the company has a known open role
  4. freshness     never emailed before

Startup is decided on provenance, not on guesswork about the name: a YC batch
string or a source that is a specific VC's portfolio crawl means someone
funded it recently and it is small. Everything else is unranked rather than
assumed large -- absence of a batch is not evidence of size.

Usage:
  python3 next_batch.py                    # top 30
  python3 next_batch.py -n 50
  python3 next_batch.py --tier published   # restrict to one evidence tier
  python3 next_batch.py --csv batch.csv
"""
import argparse
import csv
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "turso-full.db")

# Sources that are a specific investor's portfolio, or an accelerator batch.
# A company appearing in one is small and recently funded by construction.
STARTUP_SOURCES = (
    "yc-directory-crawler", "yc-jobs-crawler", "yc-scraping", "yc_import",
    "accel-scraping", "accel-india-scraping", "elevation-capital-scraping",
    "nexus-venture-partners-scraping", "nexus-scraping", "antler-india-scraping",
    "peak-xv-partners-formerly-sequoia-india-scraping", "westbridge-capital-scraping",
    "india-quotient-scraping", "lightspeed-india-scraping", "kalaari-capital-scraping",
    "3one4-capital-scraping", "z47-formerly-matrix-partners-india-scraping",
    "stellaris-venture-partners-scraping", "artha-venture-fund-scraping",
    "bessemer-venture-partners-india-scraping", "chiratae-ventures-scraping",
    "blume-ventures-scraping", "venture-highway-scraping", "8i-ventures-scraping",
    "fundamentum-partnership-scraping", "51-Startups-raised-money",
)

# Companies large enough that a cold note to a shared mailbox goes nowhere.
# Deliberately short and literal -- this is not a size model, just the handful
# that actually appear in this database.
NOT_STARTUPS = {
    "google.com", "amazon.jobs", "amazon.com", "microsoft.com", "apple.com",
    "meta.com", "facebook.com", "netflix.com", "linkedin.com", "adobe.com",
    "salesforce.com", "oracle.com", "ibm.com", "intel.com", "nvidia.com",
    "uber.com", "airbnb.com", "stripe.com", "shopify.com", "etsy.com",
    "zomato.com", "swiggy.com", "paytm.com", "flipkart.com", "ola.com",
    "infosys.com", "tcs.com", "wipro.com", "accenture.com", "cognizant.com",
    "bbc.co.uk", "stanford.edu", "uchicago.edu", "anthropic.com", "openai.com",
}

TIER_LABEL = {
    1: "published + probed",
    2: "probed",
    3: "published",
    4: "list",
}


def build(con, limit: int, tier_filter: str | None, include_sent: bool):
    sent = set()
    p = os.path.join(HERE, "sent_log.json")
    if os.path.exists(p):
        sent = {e.lower() for e in json.load(open(p)).get("sent", [])}

    rows = con.execute("""
        WITH ranked AS (
            SELECT ct.id, ct.email, ct.name, ct.role, ct.email_confidence conf,
                   ct.email_provenance prov, ct.email_verified ver, ct.priority,
                   co.name company, co.domain, co.batch, co.source co_source,
                   COALESCE(co.open_roles,0) open_roles, co.industry,
                   ROW_NUMBER() OVER (
                       PARTITION BY ct.company_id
                       ORDER BY ct.email_confidence DESC, ct.priority DESC, ct.id ASC
                   ) rn
            FROM contacts ct JOIN companies co ON ct.company_id = co.id
            WHERE ct.is_invalid = 0 AND ct.email IS NOT NULL AND ct.email != ''
              AND COALESCE(ct.email_confidence,0) >= 75
        )
        SELECT * FROM ranked WHERE rn = 1
    """).fetchall()

    out = []
    for r in rows:
        addr = r["email"].lower()
        if not include_sent and addr in sent:
            continue

        prov, ver = r["prov"] or "", r["ver"] or 0
        if prov == "published" and ver:
            tier = 1
        elif prov == "verified_guess" or (ver and prov != "published"):
            tier = 2
        elif prov in ("published", "researched"):
            tier = 3
        else:
            tier = 4
        if tier_filter and TIER_LABEL[tier] != tier_filter:
            continue

        domain = (r["domain"] or "").lower()
        is_startup = (
            domain not in NOT_STARTUPS
            and r["priority"] >= 0
            and (bool((r["batch"] or "").strip()) or (r["co_source"] or "") in STARTUP_SOURCES)
        )
        out.append({
            "id": r["id"], "email": addr, "name": r["name"] or "",
            "role": r["role"] or "", "company": r["company"] or domain,
            "domain": domain, "batch": (r["batch"] or "").strip(),
            "conf": r["conf"], "tier": tier, "tier_label": TIER_LABEL[tier],
            "startup": is_startup, "open_roles": r["open_roles"],
            "industry": (r["industry"] or "")[:28],
        })

    out.sort(key=lambda d: (d["tier"], not d["startup"], -d["open_roles"], -d["conf"]))
    return out[:limit], len(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--limit", type=int, default=30)
    ap.add_argument("--tier", default=None,
                    choices=list(TIER_LABEL.values()))
    ap.add_argument("--include-sent", action="store_true")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=60)
    con.row_factory = sqlite3.Row
    batch, total = build(con, args.limit, args.tier, args.include_sent)

    print(f"{total} contacts eligible; showing the top {len(batch)}\n")
    print(f"{'#':<4}{'email':<38}{'company':<26}{'batch':<8}{'evidence':<20}{'roles':>6}")
    print("-" * 104)
    for i, d in enumerate(batch, 1):
        star = "*" if d["startup"] else " "
        print(f"{i:<4}{d['email'][:36]:<38}{star}{d['company'][:24]:<25}"
              f"{d['batch'][:7]:<8}{d['tier_label']:<20}{d['open_roles']:>6}")
    print("-" * 104)
    print("* = startup (YC batch or a VC portfolio source)")

    n_star = sum(1 for d in batch if d["startup"])
    print(f"\nin this batch: {n_star} startups, {len(batch)-n_star} other")
    for t in sorted({d['tier'] for d in batch}):
        n = sum(1 for d in batch if d["tier"] == t)
        print(f"  {TIER_LABEL[t]:<22}{n:>4}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(batch[0].keys()))
            w.writeheader()
            w.writerows(batch)
        print(f"\nwritten to {args.csv}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
