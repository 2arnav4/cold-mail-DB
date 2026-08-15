#!/usr/bin/env python3
"""daily_drafts.py -- research the next N companies and write one personal email each.

This replaces the assistant-in-the-loop drafting step. Everything here is
mechanical or done by Groq, so a full 30-email morning run costs no Claude
tokens at all.

Pipeline, in order:

  1. select    the next N contacts the sender can actually reach. This calls
               send_emails.fetch_contacts(), which already enforces one contact
               per company, drops role inboxes (help@, support@, ...) and gates
               on min_confidence, so selection here can never disagree with what
               the sender will do later. Anything already in sent_log.json or
               marked SENT/DONE in send_queue is dropped.
  2. research  a one-line self-description per company, from companies.description
               if it is already stored, otherwise fetched live from the homepage
               <head>. Cached back into the database, so the second run over the
               same company costs nothing.
  3. write     Groq turns (company, blurb, sector) into a subject and a two or
               three sentence opener. Only these two fields vary per company;
               everything else is assembled from a fixed template.
  4. validate  exactly four links, globally unique subject, no em-dash, no
               invented statistics. A draft that fails is retried once, then
               dropped rather than shipped broken.
  5. write out a drafts JSON that run_batch.sh loads unchanged.

Deliberately NOT done here: sending. That stays in run_batch.sh, which owns the
daily cap, pacing, bounce scan and tracker sync.

Usage:
  python3 daily_drafts.py                 # 30 drafts -> drafts-YYYY-MM-DD.json
  python3 daily_drafts.py --count 10
  python3 daily_drafts.py --dry-run       # print, write nothing, no DB writes
"""
import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import send_emails as se                                  # noqa: E402
from pipeline.enrich_descriptions import fetch_blurb      # noqa: E402

# Only these four provenances are drafted for. `list` is excluded on evidence,
# not on principle: 381 hard bounces in 1209 sends is 31.5%, and it alone pushed
# the all-time rate to 25.8%. Every category below has bounced zero times.
SAFE_PROVENANCE = {"verified_guess", "published", "pending_recheck", "researched"}

GROQ_MODEL = "llama-3.3-70b-versatile"

PORTFOLIO = "https://arnav24.tech"
RESUME = "https://www.arnav24.tech/resume.pdf"

# Two project links per email, picked by sector, so the projects shown are the
# ones that plausibly relate to what the company builds.
PROJECTS = {
    "pulse": "Pulse (https://pulse-nu-liard.vercel.app/), a Node/Express backend with "
             "Postgres, JWT auth and rate limiting across 14 endpoints",
    "lucent": "Lucent (https://lucent-fintech-psi.vercel.app/), a finance dashboard "
              "pulling live data from three market APIs and handling their rate limits "
              "and inconsistent payloads",
    "helper": "Student Helper (https://student-helper-yaye.vercel.app/), an academic "
              "platform serving 150+ students with notes sharing, a swap marketplace "
              "and role-based access",
}
SECTOR_PROJECTS = {
    "fintech":    ("lucent", "pulse"),
    "b2b-saas":   ("pulse", "lucent"),
    "devtools":   ("pulse", "lucent"),
    "consumer":   ("helper", "pulse"),
    "healthtech": ("pulse", "helper"),
    "web3":       ("lucent", "pulse"),
}
DEFAULT_PROJECTS = ("pulse", "helper")

INTRO = "I'm a third-year CSE student in Delhi, graduating 2028."
CLOSER = "Would like to be considered for a backend internship."
RESOURCES = (f"Resources:\n• Portfolio: {PORTFOLIO}\n• Resume: {RESUME}\n\nArnav")

PROMPT = """You are writing the opening of a cold email from an Indian CS undergraduate \
asking for a backend engineering internship. The recipient is the founder of this company.

Company: {company}
What they do: {blurb}
Sector: {sector}

Write TWO things:
1. "subject" -- a lowercase-ish specific subject line, 4 to 9 words, naming the concrete \
engineering problem this company has. It must NOT contain the company name. It must not \
look like a mail merge. Examples of the right register: "on ranking rather than retrieval", \
"about fee schedules that never match", "and inventory that moves in minutes".
2. "opener" -- two or three sentences naming a real ENGINEERING tension implied by what \
they do. Show you understand the hard part of their problem.

The single most important rule: DO NOT DESCRIBE WHAT THE COMPANY DOES. They know. \
Describing their product back at them is the mark of a mass email and gets deleted. \
Instead, name the specific technical tradeoff hiding underneath it.

Study these. Each one takes an obvious business fact and names the non-obvious \
engineering problem inside it:

- Open source CRM -> "Twenty being open source is the reason I'm writing. The code gets \
read, and TypeScript, React and Node are exactly what I build in."
- LLM evaluation tool -> "Evaluation and observability for LLMs is hard for a reason \
traditional APM never had, which is that the same input can be correct twice in different \
ways. Deciding what counts as a regression is the actual product."
- School fee collection -> "Fee collection is a reconciliation problem before it is a \
payments one. Every institution has its own fee heads, instalment logic and exceptions, \
and the ledger has to survive all of them."
- Brokerage-free property listings -> "Removing the broker means the platform inherits the \
job the broker was actually doing, which was verification. One stale or fake listing costs \
more trust than ten good ones earn."

Notice what none of them do: none open with "As X becomes more complex", none say \
"managing X can be challenging", none list the company's product categories.

Hard rules:
- Never invent statistics, user counts, funding amounts, or any number you were not given.
- Never begin with "As ", "Managing ", "Building ", "With the rise of", or "In today's".
- Do not restate the company description you were given. Go one level beneath it.
- No em-dashes anywhere. Use commas or full stops.
- No links, no greeting, no sign-off, no bullet points.
- Do not flatter. Do not say "I was impressed" or "I love what you're building".
- Plain direct English.

Respond with ONLY this JSON, no fences, no prose:
{{"subject": "...", "opener": "..."}}"""

# Phrases that mean the model invented a metric. Cheaper to reject and retry
# than to send a founder a confident wrong number about their own company.
FABRICATION_RE = re.compile(
    r"\b\d[\d,\.]*\s*(million|billion|crore|lakh|k\b|m\b|bn\b|%|percent|users|customers|"
    r"employees|countries|cities|stores|merchants)", re.I)


def _client():
    from groq import Groq
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        sys.exit("ERROR: GROQ_API_KEY not set. It belongs in .env next to this file.")
    return Groq(api_key=key)


def select(count: int) -> list:
    """The next `count` contacts, in the sender's own ranking order.

    fetch_contacts() is the single source of truth for who is reachable. Doing
    the selection with a hand-written query here is how the last batch ended up
    with 14 drafts addressed to people the sender could never surface.
    """
    excluded = se.sent_index(se.load_log(se.CONFIG["log_path"]))
    excluded |= se.contacted_in_db(se.CONFIG["db_path"])
    out = []
    for c in se.fetch_contacts(se.CONFIG["db_path"]):
        if len(out) >= count:
            break
        if c["email_provenance"] not in SAFE_PROVENANCE:
            continue
        if se.already_sent(excluded, c["contact_email"]):
            continue
        out.append(c)
    return out


def research(contact: dict, con, dry: bool) -> str:
    """One line about the company. Prefers what is already stored."""
    # CONTACT_QUERY does not select co.description, so read it here rather than
    # widening a query the sender also depends on.
    row = con.execute("SELECT COALESCE(description, '') FROM companies WHERE id = ?",
                      (contact["company_id"],)).fetchone()
    stored = (row[0] if row else "").strip()
    if len(stored) > 30:
        return stored[:300]
    domain = (contact.get("company_domain") or "").strip()
    blurb = fetch_blurb(domain) if domain else None
    if blurb and not dry:
        con.execute("UPDATE companies SET description = ? WHERE id = ?",
                    (blurb, contact["company_id"]))
        con.commit()
    # An unreachable homepage is normal, not an error. The sector plus the
    # company name is still enough for a usable opener.
    return (blurb or contact.get("company_industry") or contact.get("company_sector")
            or "No public description available.")[:300]


def compose(contact: dict, subject: str, opener: str) -> str:
    sector = (contact.get("company_sector") or "").lower()
    a, b = SECTOR_PROJECTS.get(sector, DEFAULT_PROJECTS)
    first = (contact.get("contact_name") or "there").split()[0]
    return (f"Hi {first},\n\n{opener}\n\n{INTRO} I built {PROJECTS[a]}. "
            f"Also {PROJECTS[b]}.\n\n{CLOSER}\n\n{RESOURCES}")


def validate(subject: str, body: str, seen_subjects: set) -> str | None:
    """Return an error string, or None when the draft is shippable."""
    links = re.findall(r'https?://[^\s<>")]+', body)
    if len(links) != 4:
        return f"{len(links)} links, want exactly 4"
    if sum(1 for u in links if "arnav24.tech" not in u) != 2:
        return "project links != 2"
    if "github.com" in body or "linkedin" in body.lower():
        return "contains github/linkedin"
    if "—" in body or "—" in subject:
        return "em-dash"
    if subject.strip().lower() in seen_subjects:
        return "duplicate subject"
    if FABRICATION_RE.search(body):
        return "looks like an invented statistic"
    if not (15 <= len(subject) <= 80):
        return f"subject length {len(subject)}"
    # The openings a model falls back on when it has nothing specific to say.
    # Cheaper to retry at a higher temperature than to send filler.
    opener = body.split("\n\n")[1] if "\n\n" in body else body
    if re.match(r"^(As |Managing |Building |With the rise|In today)", opener.strip(), re.I):
        return "generic opening phrase"
    return None


def past_subjects() -> set:
    """Every subject already used, so today's 30 cannot repeat any of them."""
    seen = set()
    live = os.path.join(HERE, "personalized_emails.local.json")
    if os.path.exists(live):
        for v in json.load(open(live, encoding="utf-8")).values():
            if isinstance(v, dict) and v.get("subject"):
                seen.add(v["subject"].strip().lower())
    for name in os.listdir(HERE):
        if name.startswith("drafts-") and name.endswith(".json"):
            try:
                for v in json.load(open(os.path.join(HERE, name), encoding="utf-8")).values():
                    if isinstance(v, dict) and v.get("subject"):
                        seen.add(v["subject"].strip().lower())
            except Exception:
                continue
    return seen


def ask(client, contact, blurb, temperature):
    prompt = PROMPT.format(company=contact["company_name"], blurb=blurb,
                           sector=contact.get("company_sector") or "unknown")
    resp = client.chat.completions.create(
        model=GROQ_MODEL, temperature=temperature, max_tokens=400,
        messages=[{"role": "user", "content": prompt}])
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):                      # models fence JSON despite instructions
        raw = raw.strip("`")
        raw = raw[4:] if raw.startswith("json") else raw
    data = json.loads(raw.strip())
    return data["subject"].strip(), data["opener"].strip()


def main():
    ap = argparse.ArgumentParser(description="Research and draft the next N cold emails")
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--out", default=None, help="output path (default drafts-<today>.json)")
    ap.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    args = ap.parse_args()

    targets = select(args.count)
    if not targets:
        sys.exit("Nothing to draft: no unsent contacts in the safe provenance set.")
    print(f"Drafting {len(targets)} emails (asked for {args.count})")

    client = _client()
    con = sqlite3.connect(se.CONFIG["db_path"], timeout=60)
    seen = past_subjects()
    drafts, failed = {}, []

    for i, c in enumerate(targets, 1):
        email = c["contact_email"].strip().lower()
        blurb = research(c, con, args.dry_run)
        subject = opener = None
        for attempt, temp in enumerate((0.7, 0.9), 1):
            try:
                subject, opener = ask(client, c, blurb, temp)
            except Exception as e:
                print(f"  [{i:02d}/{len(targets)}] {email:34} GROQ FAILED: {e}")
                subject = None
                continue
            body = compose(c, subject, opener)
            err = validate(subject, body, seen)
            if err is None:
                drafts[email] = {"subject": subject, "body": body}
                seen.add(subject.strip().lower())
                print(f"  [{i:02d}/{len(targets)}] {email:34} {c['company_name'][:18]:18} OK")
                break
            print(f"  [{i:02d}/{len(targets)}] {email:34} retry {attempt}: {err}")
            subject = None
        if subject is None:
            failed.append(email)

    con.close()
    print(f"\n  written: {len(drafts)}   failed: {len(failed)}")
    for f in failed:
        print(f"    dropped: {f}")

    if args.dry_run:
        for e, v in drafts.items():
            print("\n" + "=" * 70)
            print(f"{e}\nSUBJECT: {v['subject']}\n\n{v['body']}")
        return

    out = args.out or os.path.join(
        HERE, f"drafts-{_dt.date.today().isoformat()}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(drafts, fh, indent=2, ensure_ascii=False)
    print(f"\n  -> {out}")
    print(f"  send with:  DRAFTS={os.path.basename(out)} ./run_batch.sh {len(drafts)} 7")


if __name__ == "__main__":
    main()
