# exec_finder — scoped demo

A 3-stage pipeline that takes a company name + domain and tries to resolve
a leadership contact's email, entirely from **public/self-disclosed
information**:

1. **`llm_lookup.py`** — asks Groq's LLM (from training knowledge only, no
   web access) who plausibly holds Founder/CEO/Head-of-HR roles at the
   company, with a self-reported confidence.
2. **`site_scraper.py`** — crawls a handful of pages on the company's *own*
   domain (`/about`, `/team`, `/leadership`, `/press`, `/contact`, ...) for
   emails/names the company has already chosen to publish. Respects
   `robots.txt` and rate-limits requests.
3. **`email_finder.py`** — for any name still without a found email,
   generates common corporate email patterns (`first.last@`, `flast@`, etc.)
   and verifies each candidate via MX + SMTP RCPT probe (reusing
   `../email_verify.py`), rather than sending anything.

`pipeline.py` runs all three stages and writes a CSV where every row
records **how** an email was resolved (`published:<url>`,
`pattern_verified:<method>`) or that it wasn't
(`unresolved (tried N patterns)`), on purpose — the goal here is to make the
failure modes visible, not to paper over them.

## What this deliberately does NOT do

- No LinkedIn scraping, no scraping of any site other than the target
  company's own domain.
- No web-wide search for a named individual's personal email. Stage 3 only
  verifies deliverability of *guessed* addresses — it never looks up
  someone's real address.
- No generic-inbox harvesting (`info@`, `hello@`, etc. are flagged
  separately, not treated as a "found contact").

## Setup

```bash
cd exec_finder
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in GROQ_API_KEY (free tier: console.groq.com)
```

## Usage

```bash
python pipeline.py --company "Acme Inc" --domain acme.com
# or batch:
python pipeline.py --input companies.csv --output results.csv
```

## Where this actually breaks (the point of the demo)

- **Stage 1 hallucination**: for any company that isn't extremely
  well-known, the LLM either returns nothing (correct behavior, per the
  prompt) or names a person who's outdated/wrong. There's no way to tell
  which from the output alone — `llm_confidence` is a self-report, not a
  fact-check.
- **Stage 2 coverage**: most company sites don't publish individual
  emails at all — only generic ones, or none. Expect this stage to come up
  empty for a large fraction of real companies.
- **Stage 3 pattern ceiling**: the pattern list only covers common English
  naming conventions. Companies using initials-only schemes, numbered
  disambiguation (`jsmith2@`), or non-Latin names will silently fail even
  when SMTP verification is working correctly.
- **Compounding error**: since stage 3 depends on a name from stage 1, a
  hallucinated name produces a *verified-deliverable* email for the wrong
  person if that pattern happens to match a real mailbox (e.g. a generic
  catch-all domain). Deliverable is not the same as correct — worth
  highlighting explicitly when presenting this.
