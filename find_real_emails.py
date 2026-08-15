#!/usr/bin/env python3
"""find_real_emails.py — replace guessed addresses with published ones.

Why this exists: on 2026-08-02, 9 of 15 sends bounced. Every hard bounce was a
Google-hosted domain, where the SMTP probe cannot tell a real mailbox from a
missing one -- Google accepts RCPT TO at the gateway and rejects at delivery.
181 of 186 "verified" addresses were Google-hosted, so verification was telling
us essentially nothing.

The deeper cause is that the addresses were never found, only guessed:
4,839 of 6,968 contacts are `firstname@domain` at confidence 60-65, and exactly
6 sit above 90. No verifier can rescue a guess on a domain it cannot probe.

So: read what companies publish about themselves. A `mailto:` on a team page is
evidence in a way a pattern guess never is.

Scope, deliberately narrow (inherited from exec_finder/site_scraper.py):
  - only the company's own domain, a fixed list of About/Team/Contact paths
  - robots.txt respected, rate-limited between requests
  - no search engines, no third-party directories, no LinkedIn scraping

Confidence written back:
  100  mailto: link            -- the company published it as a link
   95  address in page text    -- published, but parsed out of prose
Both beat the 60-65 the guessed rows carry, which is the point.

Usage:
  python3 find_real_emails.py --limit 25            # report only
  python3 find_real_emails.py --limit 25 --apply
  python3 find_real_emails.py --limit 200 --apply --workers 8 --include-generic
"""
import argparse
import concurrent.futures as futures
import re
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "exec_finder"))

from site_scraper import scrape_company_site  # noqa: E402

sys.path.insert(0, HERE)
from pipeline.attribution import corroborates, is_label  # noqa: E402

DB = os.path.join(HERE, "turso-full.db")

# Roles worth writing down when the page gives us one. Anything else lands as
# NULL rather than a guess -- an invented role is worse than a blank.
ROLE_HINTS = [
    ("founder", "Founder"), ("co-founder", "Co-Founder"), ("ceo", "CEO"),
    ("cto", "CTO"), ("chief", "Chief"), ("head", "Head"), ("director", "Director"),
    ("recruit", "Recruiting"), ("talent", "Talent"), ("people", "People"),
    ("hr", "HR"), ("engineer", "Engineering"),
]


def sent_emails() -> set:
    path = os.path.join(HERE, "sent_log.json")
    if not os.path.exists(path):
        return set()
    return {e.lower() for e in json.load(open(path)).get("sent", [])}


def ensure_columns(con):
    cols = {r["name"] for r in con.execute("PRAGMA table_info(companies)")}
    added = []
    if "site_scraped_at" not in cols:
        con.execute("ALTER TABLE companies ADD COLUMN site_scraped_at DATETIME")
        added.append("companies.site_scraped_at")
    ccols = {r["name"] for r in con.execute("PRAGMA table_info(contacts)")}
    if "source_url" not in ccols:
        con.execute("ALTER TABLE contacts ADD COLUMN source_url VARCHAR(512)")
        added.append("contacts.source_url")
    if added:
        con.commit()
        print("added columns:", ", ".join(added))


# Company sites carry addresses that are not the company's: customer logos,
# testimonials, integration docs, and copy-paste placeholder text. Scraping
# orangeslice.ai yielded someone@stripe.com, someone@figma.com and
# someone@datadoghq.com -- real people at other companies. Mailing them a pitch
# about Orange Slice would be considerably worse than a bounce.
PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "email.com",
    "domain.com", "yourdomain.com", "yourbrand.com", "yourcompany.com",
    "company.com", "acme.com", "acme.org", "acme.dev", "mail.com",
    "sentry.io", "wixpress.com", "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "yahoo.com", "sentry-next.wixpress.com",
}
PLACEHOLDER_LOCALS = {
    "you", "user", "test", "name", "email", "yourname", "someone",
    "firstname", "username", "example", "your", "no-reply", "noreply",
}

# Never mail these. abuse@ is the address people use to REPORT spam, so cold
# outreach to it is a direct request to be listed. The rest are legal, security
# and infrastructure mailboxes where an internship pitch is noise at best.
BLOCKED_LOCALS = {
    "abuse", "postmaster", "hostmaster", "webmaster", "security", "privacy",
    "legal", "dpo", "gdpr", "compliance", "noc", "unsubscribe", "bounce",
    "mailer-daemon", "spam", "phishing", "root", "billing", "invoice",
    "accounts", "accounting", "donotreply",
}

# Lower rank sends first. A founders@ address at a seed-stage startup is read
# by a founder; sales@ lands in a CRM and support@ opens a ticket. careers@ is
# the single most on-target mailbox for an internship enquiry.
LOCAL_RANK = [
    ({"careers", "jobs", "join", "hiring", "recruiting", "talent", "work"}, 1),
    ({"founders", "founder", "team", "hello", "hey", "hi", "people"}, 2),
    ({"contact", "info", "enquiries", "inquiries", "general", "office"}, 3),
    ({"press", "media", "partnerships", "partners", "marketing"}, 5),
    ({"sales", "support", "help", "admin", "service", "customercare"}, 6),
]


def local_rank(email: str) -> int:
    """0 for a personal address, higher for progressively worse shared ones."""
    local = email.split("@", 1)[0].lower()
    for group, rank in LOCAL_RANK:
        if local in group:
            return rank
    return 0  # looks like a person's name
EMAIL_OK = re.compile(r"^[\w.+\-]+@[\w\-]+(\.[\w\-]+)*\.[a-zA-Z]{2,}$")


def normalise(email: str) -> str:
    """Trailing dots come straight off the page ('founders@example.bio.') and
    a stored address with one will never match anything. The mailto: prefix
    survives some malformed href attributes, and code samples on docs pages
    contribute things like git@github.com and password@prod.db."""
    return email.strip().lower().removeprefix("mailto:").strip(".,;:<>()[]\"'")


def same_company(email_domain: str, company_domain: str) -> bool:
    """True when the address plausibly belongs to this company.

    Accepts exact matches, subdomains either way, and a shared leading label so
    that mail.acme.com, acme.io and acme.co.uk all count as acme. Deliberately
    strict: a false accept means emailing a stranger about someone else's
    company, which is far more costly than a missed contact.
    """
    e = email_domain.lower().strip().rstrip(".").removeprefix("www.")
    c = company_domain.lower().strip().rstrip(".").removeprefix("www.")
    if not e or not c:
        return False
    if e == c or e.endswith("." + c) or c.endswith("." + e):
        return True
    e_core, c_core = e.split(".")[0], c.split(".")[0]
    return len(e_core) > 3 and len(c_core) > 3 and (e_core in c_core or c_core in e_core)


def acceptable(email: str, company_domain: str) -> tuple[bool, str]:
    """Returns (keep, reason_if_rejected)."""
    if not EMAIL_OK.match(email):
        return False, "malformed"
    local, _, domain = email.partition("@")
    if domain in PLACEHOLDER_DOMAINS:
        return False, "placeholder domain"
    if local in PLACEHOLDER_LOCALS:
        return False, "placeholder local part"
    if local in BLOCKED_LOCALS:
        return False, f"{local}@ is never a valid outreach target"
    if not same_company(domain, company_domain):
        return False, f"belongs to {domain}, not {company_domain}"
    return True, ""


def guess_role(name_hint: str, email: str) -> str | None:
    blob = f"{name_hint} {email}".lower()
    for needle, label in ROLE_HINTS:
        if needle in blob:
            return label
    return None


# Words that make a capitalised phrase a call to action rather than a person.
# Tested per token, never as a substring: "Contact Us" must be rejected while
# "Kilian Justus" and "Walter Raftus" are kept, and a substring test on "us"
# fails both of those.
NOT_A_PERSON = {
    "contact", "support", "sales", "info", "information", "enquiries", "enquiry",
    "inquiries", "inquiry", "team", "help", "helpdesk", "careers", "jobs",
    "hello", "message", "mail", "email", "touch", "customer", "customers",
    "media", "press", "admin", "office", "service", "services", "billing",
    "partner", "partners", "partnership", "partnerships", "feedback", "demo",
    "us", "we", "here", "now", "today", "more", "learn", "book", "get", "talk",
    "reach", "call", "write", "join", "subscribe", "apply", "request",
}


def clean_name(name_hint: str, email: str) -> str | None:
    """A name_hint is whatever text sat near the address, so it is often a
    sentence fragment. Only keep it when it actually looks like a person's name.

    Shape alone is not enough: "Contact Us", "Email Us" and "Contact Support"
    are all two capitalised words and all passed the original test, which put 131
    of them in the database as people. The template greets `{{first_name}}`, so
    those became "Hi Contact," in a real cold email -- worse than no greeting.
    """
    if not name_hint:
        return None
    hint = " ".join(name_hint.split())
    if "@" in hint or len(hint) > 48:
        return None
    words = [w for w in hint.split() if w]
    if not 2 <= len(words) <= 3:
        return None
    if not all(w[0].isupper() and w.replace("-", "").replace("'", "").isalpha() for w in words):
        return None
    if any(w.lower() in NOT_A_PERSON for w in words):
        return None

    # Everything above filters by exclusion, and three rounds of that on real
    # sites showed it does not converge -- "Sign In", "Privacy Terms" and
    # "Learn More" all cleared the list above and were written as people.
    #
    # So the address gets a vote. A personal mailbox carries some of its
    # owner's name; a navigation label sitting beside info@ shares nothing with
    # it. is_label runs first because a department mailbox is named after its
    # department, so "Contact Us" corroborates contact@ perfectly and only
    # vocabulary can reject it.
    if is_label(hint):
        return None
    if not corroborates(hint, email):
        return None
    return hint


def scrape_one(row) -> dict:
    """Runs in a worker thread. One company, its own pages, serially."""
    try:
        found = scrape_company_site(row["domain"])
    except Exception as e:
        return {"company_id": row["id"], "name": row["name"], "error": str(e), "found": []}
    return {"company_id": row["id"], "name": row["name"], "error": None, "found": found}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--workers", type=int, default=6,
                    help="companies scraped in parallel; each company's own pages stay serial")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-generic", action="store_true",
                    help="also store careers@/hello@ style addresses. For internship outreach "
                         "these are legitimate targets and far likelier to be real than a guess.")
    ap.add_argument("--rescrape", action="store_true",
                    help="ignore site_scraped_at and scrape companies again")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if args.apply:
        ensure_columns(con)

    has_col = "site_scraped_at" in {r["name"] for r in con.execute("PRAGMA table_info(companies)")}
    unscraped = "" if (args.rescrape or not has_col) else "AND co.site_scraped_at IS NULL"

    rows = con.execute(f"""
        SELECT co.id, co.name, co.domain, MAX(ct.priority) AS pri
        FROM companies co JOIN contacts ct ON ct.company_id = co.id
        WHERE co.domain IS NOT NULL AND TRIM(co.domain) <> ''
          AND ct.is_invalid = 0
          {unscraped}
        GROUP BY co.id
        ORDER BY pri DESC, co.id ASC
        LIMIT ?
    """, (args.limit,)).fetchall()

    print(f"companies to scrape: {len(rows)}")
    if not rows:
        print("nothing to do (all scraped? try --rescrape)")
        return
    if not args.apply:
        for r in rows[:10]:
            print(f"  {r['pri']}  {r['name'][:30]:30} {r['domain']}")
        print("\n[REPORT ONLY] rerun with --apply to scrape and write")
        return

    backup = f"{DB}.backup-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(DB, backup)
    print(f"backup: {os.path.basename(backup)}\n")

    already = sent_emails()
    existing = {(r[0] or "").lower() for r in con.execute("SELECT email FROM contacts")}
    now = datetime.now().isoformat()
    inserted = personal = generic = rejected = 0
    companies_with_hits = 0
    by_id = {r['id']: r['domain'] for r in rows}

    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(scrape_one, rows):
            con.execute("UPDATE companies SET site_scraped_at = ? WHERE id = ?",
                        (now, res["company_id"]))
            if res["error"]:
                print(f"  ERR   {res['name'][:26]:26} {res['error'][:50]}")
                continue

            keep = [f for f in res["found"]
                    if args.include_generic or not f["is_generic"]]
            if not keep:
                print(f"  none  {res['name'][:26]:26}")
                continue

            company_domain = by_id[res["company_id"]]
            hit_this_company = False
            for f in keep:
                email = normalise(f["email"])
                keep_it, why = acceptable(email, company_domain)
                if not keep_it:
                    rejected += 1
                    print(f"  reject   {res['name'][:22]:22} {email[:34]:34} ({why})")
                    continue
                low = email
                if low in existing or low in already:
                    continue
                existing.add(low)
                hit_this_company = True
                confidence = 100 if f["source_url"] and "mailto" not in f else 95
                con.execute(
                    """INSERT OR IGNORE INTO contacts
                       (company_id, name, role, email, email_confidence, email_verified,
                        is_invalid, created_at, source, source_url, scraped_pattern, priority)
                       VALUES (?, ?, ?, ?, ?, 0, 0, ?, 'site_scrape', ?, 'found', ?)""",
                    (res["company_id"], clean_name(f.get("name_hint", ""), email),
                     guess_role(f.get("name_hint", ""), email), email, confidence,
                     now, f.get("source_url"), 7000),
                )
                inserted += 1
                if f["is_generic"]:
                    generic += 1
                else:
                    personal += 1
                tag = "generic" if f["is_generic"] else "PERSONAL"
                print(f"  {tag:8} {res['name'][:22]:22} {email}")
            if hit_this_company:
                companies_with_hits += 1

    con.commit()
    print(f"\n[APPLIED] {len(rows)} companies scraped, {companies_with_hits} had usable addresses")
    print(f"  inserted {inserted} contacts ({personal} personal, {generic} generic) at priority 7000")
    print(f"  rejected {rejected} addresses (wrong company, placeholder, or malformed)")
    total = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0").fetchone()[0]
    high = con.execute("SELECT COUNT(*) FROM contacts WHERE is_invalid=0 AND email_confidence>=95").fetchone()[0]
    print(f"  active contacts: {total}, of which confidence>=95: {high}")


if __name__ == "__main__":
    main()
