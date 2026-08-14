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
sys.path.insert(0, HERE)

DB = os.path.join(HERE, "turso-full.db")

# Words that mean a mailbox belongs to a function, not a person. Matched against
# the tokens of the local part, so `store-support`, `customer.experience` and
# `billing+es` are all caught -- an exact-match set missed every one of those.
FUNCTION_WORDS = {
    "support", "help", "helpdesk", "service", "care", "careteam", "success",
    "complaints", "feedback", "billing", "invoice", "invoices", "accounts",
    "accounting", "payments", "refunds", "returns", "orders", "shipping",
    "sales", "revenue", "bd", "biz", "business", "partnerships", "partner",
    "press", "media", "marketing", "advertise", "affiliates", "wholesale",
    "drivers", "driver", "investorrelations", "investors", "underwriting",
    "claims", "admissions", "students", "customers", "customer", "experience",
    "trust", "safety", "store", "shop", "legal", "privacy", "security",
    "applicant", "application", "apply", "noreply", "no-reply", "donotreply",
}

# The mailboxes actually worth an internship note, best first.
RIGHT_TEAM = ["careers", "jobs", "hiring", "recruiting", "talent", "work", "join"]
FOUNDER_BOX = ["founders", "founder", "team", "hello", "hey", "hi", "contact", "info"]

TOKEN_SPLIT = __import__("re").compile(r"[.\-_+]+")

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


def contacted_in_db(db_path: str) -> set:
    """Addresses the send_queue already marked SENT/DONE, lowercased.

    sent_log.json is not the whole history. An address can be recorded as sent
    in the database without ever reaching that file -- a run that crashed after
    the queue update, or a send made before the log existed. send_emails.py has
    always excluded both; this preview excluded only the log, and so listed
    people who would never actually be mailed.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT email FROM contacts WHERE id IN "
                "(SELECT contact_id FROM send_queue WHERE status IN ('SENT', 'DONE'))"
            ).fetchall()
        finally:
            conn.close()
        return {r[0].lower() for r in rows if r[0]}
    except Exception as e:
        print(f"  Warning: could not read send_queue history: {e}")
        return set()


def evidence_tier(provenance: str | None, probed_at) -> int:
    """How much the address is trusted not to bounce. Lower is better.

    email_verified alone is not evidence: 730 rows carry it from probes run
    before the IPv4 fix and before catch-all detection worked on Google-hosted
    domains. Only probe_checked_at means a probe ran under the current code.
    Anything else falls back to how the address was found, which for
    `published` does not depend on probing at all.
    """
    prov = provenance or ""
    fresh = bool(probed_at)
    if prov == "published" and fresh:
        return 1
    if fresh and prov == "verified_guess":
        return 2
    if prov in ("published", "researched"):
        return 3
    return 4


def looks_like_startup(domain, batch, source, priority) -> bool:
    """Small and recently funded, decided on provenance rather than guesswork.

    A YC batch string or a specific VC's portfolio crawl means someone funded
    it recently and it is small. Everything else is unranked rather than
    assumed large -- absence of a batch is not evidence of size.
    """
    return (
        (domain or "").lower() not in NOT_STARTUPS
        and (priority or 0) >= 0
        and (bool((batch or "").strip()) or (source or "") in STARTUP_SOURCES)
    )


def rank_key(*, email, name, provenance, probed_at, domain, batch, source,
             priority, open_roles, confidence) -> tuple:
    """The sort key for a send batch, best first.

    Mailbox quality is the primary key, ahead of bounce tier. Everything
    reaching here is already above the send gate, so the question is no longer
    "will this bounce" but "will anyone read it" -- and a named founder that
    was probed beats support@ that was probed *and* published. Sorting by tier
    first put support@veryfi.com at #1 over a founder inbox; sorting by raw
    confidence, which is what send_emails.py did, put presales@, hr@ and
    customer-support@ in the top 30 and no named person at all.
    """
    return (
        mailbox_quality(email, name),
        evidence_tier(provenance, probed_at),
        not looks_like_startup(domain, batch, source, priority),
        -(open_roles or 0),
        -(confidence or 0),
        # Deterministic tail. Without it the ordering depends on which query the
        # rows arrived from -- Python's sort is stable, so ties keep their input
        # order, and next_batch and send_emails read the pool with two different
        # statements. They agreed on 27 of 30 before this line existed, which is
        # the worst possible outcome: close enough to look correct.
        (email or "").lower(),
    )


def mailbox_quality(email: str, name: str | None) -> int:
    """How likely this mailbox is read by someone who can act on the note.

    Lower is better. `find_real_emails.local_rank` returns 0 for anything it does
    not recognise, i.e. it assumes "unknown means a person's name" -- which put
    complaints@, store-support@ and billing+es@ at the very top of a batch. This
    tests for a person *positively* instead, and treats unrecognised multi-token
    locals as functional rather than personal.

      0  matches the contact's recorded name -- certainly a person
      1  a single plain word, no function word -- probably a first name
      2  careers@ / jobs@ / hiring@ -- the right team by definition
      3  founders@ / hello@ / contact@ -- small company, reaches a human
      9  a function mailbox -- read by a team this is not addressed to
    """
    local = email.split("@", 1)[0].lower()
    tokens = [t for t in TOKEN_SPLIT.split(local) if t]

    # Function words are checked before the name match, not after. The scraper
    # recorded "Contact Support" as the name on support@ addresses, so matching
    # the name first scored those 0 -- a perfect personal address -- and put
    # support@veryfi.com at the top of the batch.
    if any(t in FUNCTION_WORDS for t in tokens):
        return 9

    if name:
        parts = {p.lower() for p in TOKEN_SPLIT.split(name.replace(" ", ".")) if len(p) > 2}
        if parts & set(tokens):
            return 0
    if local in RIGHT_TEAM or tokens[0] in RIGHT_TEAM:
        return 2
    if local in FOUNDER_BOX:
        return 3

    # "Looks like a word" is not evidence of a person. guest-posts@,
    # taxagencies@, smartassetamp@, hotro@ and salses@ (a typo of sales) are all
    # alphabetic tokens absent from any blocklist, and all scored as personal
    # addresses -- they filled most of a 15-contact batch. A recorded name on
    # the contact row is the only positive evidence available here, so without
    # one the shape of the local part decides nothing.
    if not name:
        return 4

    if len(tokens) == 1 and tokens[0].isalpha() and 2 <= len(tokens[0]) <= 14:
        return 1
    if len(tokens) == 2 and all(t.isalpha() for t in tokens):
        return 1                      # first.last
    return 9


def build(con, limit: int, tier_filter: str | None, include_sent: bool,
          db_path: str = DB):
    sent = set()
    p = os.path.join(HERE, "sent_log.json")
    if os.path.exists(p):
        sent = {e.lower() for e in json.load(open(p)).get("sent", [])}
    # Same two sources send_emails.py excludes, in the same order.
    sent |= contacted_in_db(db_path)

    rows = con.execute("""
        WITH ranked AS (
            SELECT ct.id, ct.email, ct.name, ct.role, ct.email_confidence conf,
                   ct.email_provenance prov, ct.email_verified ver, ct.priority,
                   ct.probe_checked_at probed_at,
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

        prov = r["prov"] or ""
        tier = evidence_tier(prov, r["probed_at"])
        if tier_filter and TIER_LABEL[tier] != tier_filter:
            continue

        domain = (r["domain"] or "").lower()
        is_startup = looks_like_startup(domain, r["batch"], r["co_source"], r["priority"])
        out.append({
            "id": r["id"], "email": addr, "name": r["name"] or "",
            "role": r["role"] or "", "company": r["company"] or domain,
            "domain": domain, "batch": (r["batch"] or "").strip(),
            "conf": r["conf"], "tier": tier, "tier_label": TIER_LABEL[tier],
            "startup": is_startup, "open_roles": r["open_roles"],
            "industry": (r["industry"] or "")[:28],
            "mailbox_rank": mailbox_quality(addr, r["name"]),
            "_rank": rank_key(
                email=addr, name=r["name"], provenance=prov,
                probed_at=r["probed_at"], domain=domain, batch=r["batch"],
                source=r["co_source"], priority=r["priority"],
                open_roles=r["open_roles"], confidence=r["conf"],
            ),
        })

    # One definition of "best", in rank_key, used here and by send_emails.py.
    # This function used to re-state the ordering as a lambda, which is exactly
    # how a preview drifts away from the thing it previews.
    out.sort(key=lambda d: d["_rank"])
    for d in out:
        del d["_rank"]          # not a column anyone wants in the CSV
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
        print(f"{i:<4}{d["email"][:36]:<38}{star}{d["company"][:24]:<25}"
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
