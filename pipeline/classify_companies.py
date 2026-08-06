#!/usr/bin/env python3
"""classify_companies.py — sort companies into sectors so send_emails.py can
pick a template that actually matches what the company does.

Reads companies.description / industry / name, asks Groq to place each into one
of SECTORS, and writes the answer to companies.sector. Companies with nothing to
go on are left NULL and fall back to the generic template.

Batched 40 companies per request to keep the call count down. Run with --apply
to write; the default is a report.

Usage:
  python3 classify_companies.py            # report only
  python3 classify_companies.py --apply    # classify and write
  python3 classify_companies.py --apply --limit 200
"""
import json
import os
import re
import sqlite3
import sys
import time

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
DB = os.path.join(HERE, "turso-full.db")
BATCH = 40
MODEL = "llama-3.3-70b-versatile"

# No ai-ml bucket on purpose: the templates reference things Arnav has actually
# built, and claiming ML experience he does not have is worse than a generic email.
SECTORS = [
    "devtools",    # developer tools, infra, observability, APIs, cloud, security
    "fintech",     # payments, banking, lending, trading, insurance, accounting
    "consumer",    # marketplaces, D2C, social, travel, food, gaming, edtech
    "b2b-saas",    # sales, HR, marketing, ops, analytics, vertical SaaS
    "healthtech",  # clinical, biotech, wellness, medical devices, health data
    "web3",        # crypto, blockchain, DeFi, wallets, protocols
    "other",       # anything that genuinely does not fit above
]

PROMPT = """You classify companies into exactly one sector.

Allowed sectors: {sectors}

Rules:
- Reply with ONLY a JSON object mapping each id (as a string) to one sector.
- No prose, no code fences, no explanation.
- If a company plainly does not fit, use "other". Do not invent sectors.
- A company selling software TO developers is devtools, even if its customers
  are banks. Classify by what the product is, not who buys it.

Companies:
{companies}"""


def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def company_blurb(row) -> str:
    """Best available signal, cheapest first. Truncated because descriptions can
    run long and we pay per token."""
    bits = [row["name"]]
    if row["industry"]:
        bits.append(f"industry: {row['industry']}")
    if row["description"]:
        bits.append(row["description"][:180])
    elif row["domain"]:
        bits.append(f"domain: {row['domain']}")
    return " | ".join(b for b in bits if b)


def parse_reply(text: str) -> dict:
    """Groq usually returns clean JSON, but occasionally wraps it in a fence or
    adds a sentence. Pull the outermost object rather than trusting the shape."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def main():
    apply = "--apply" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    load_env()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    cols = {r["name"] for r in con.execute("PRAGMA table_info(companies)")}
    if "sector" not in cols:
        if apply:
            con.execute("ALTER TABLE companies ADD COLUMN sector VARCHAR(24)")
            con.commit()
            print("added companies.sector")
        else:
            print("would add column: companies.sector")

    where = "sector IS NULL AND " if "sector" in cols else ""
    rows = con.execute(f"""
        SELECT id, name, domain, industry, description FROM companies
        WHERE {where}(
            (description IS NOT NULL AND TRIM(description) <> '')
            OR (industry IS NOT NULL AND TRIM(industry) <> '')
        )
        ORDER BY id
    """).fetchall()
    if limit:
        rows = rows[:limit]

    print(f"classifiable companies: {len(rows)}")
    if not rows:
        return
    if not apply:
        print(f"would send {(len(rows) + BATCH - 1) // BATCH} Groq requests")
        print("\n[REPORT ONLY] rerun with --apply to classify and write")
        return

    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    results, failed = {}, 0
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        listing = "\n".join(f"{r['id']}: {company_blurb(r)}" for r in chunk)
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": PROMPT.format(
                    sectors=", ".join(SECTORS), companies=listing)}],
                temperature=0,
                max_tokens=1600,
            )
            parsed = parse_reply(resp.choices[0].message.content)
            for cid, sector in parsed.items():
                sector = str(sector).strip().lower()
                if sector in SECTORS:
                    results[int(cid)] = sector
        except Exception as e:
            failed += 1
            print(f"  batch at {start} failed: {e}")
            time.sleep(2)
            continue

        done = min(start + BATCH, len(rows))
        print(f"  {done}/{len(rows)} classified ({len(results)} assigned)")
        time.sleep(0.4)  # stay well inside Groq's rate limit

    con.executemany(
        "UPDATE companies SET sector = ? WHERE id = ?",
        [(s, i) for i, s in results.items()],
    )
    con.commit()

    print(f"\n[APPLIED] {len(results)} companies classified, {failed} batches failed")
    for r in con.execute(
        "SELECT COALESCE(sector,'(unclassified)') s, COUNT(*) c "
        "FROM companies GROUP BY s ORDER BY c DESC"
    ):
        print(f"  {r['c']:5}  {r['s']}")


if __name__ == "__main__":
    main()
