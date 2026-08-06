# crawler

Acquisition. Finds companies that are hiring, then reads the addresses those
companies publish about themselves.

```
discover.py ─┐
             ├─> companies ──> harvest.py ──> contacts (confidence 100/95)
hn.py       ─┘
```

## The rule this package exists to enforce

**No stage here invents an email address.**

The database arrived with 7,095 contacts, of which 6,029 were
`firstname@domain` produced by `scraper.py:infer_email()` and 129 were addresses
anyone had actually seen. That ratio is why 811 sends bounced. So `discover.py`
writes people with `email = NULL` — keeping the name, title and company, which
is what makes a later match possible — and only `harvest.py` writes an address,
only after reading it off the company's own site.

## Why Chrome, and where it stops

Chrome does two things:

1. **The rotating Algolia key.** YC's 6,124-company directory is backed by
   Algolia and mints a browser-side search key into `window.AlgoliaOpts` on each
   page load. A hardcoded key 403s the moment it rotates — which is exactly what
   happened to the first one tried here. Reading it from a live page is the only
   version that keeps working.
2. **Infinite-scroll job boards**, where the server sends page one and
   JavaScript fetches the rest.

That is all. Everything downstream is plain HTTP, because once a slug is known
the detail page is server-rendered and `requests` reads it ~40x faster than
driving a browser to the same place. Browser automation costs ~1-2 pages/minute;
the harvester does ~2 domains/second. On 8,883 domains that is the difference
between two hours and several weeks.

## Two pagination traps, both of which bit

**Algolia caps a query at 1,000 results** regardless of `nbHits`. The first
version reported "6,125 companies across 1 page" and silently returned a sixth
of the directory. Fixed by slicing on the `batch` facet — every YC batch is a
few hundred companies, comfortably under the cap, and the union is the index.

**HN's relevance search is not chronological.** Searching the title
`"Ask HN: Who is hiring?"` returned November 2025 as the newest thread when
August 2026 existed, because relevance scores text match and points, not
recency. Fixed by listing the `whoishiring` account's stories with
`search_by_date`. The same account posts "Who wants to be hired?" on the same
day — that one is job seekers advertising themselves, and is excluded.

## Politeness

Per-host, not global: many hosts in flight at once, but one request at a time to
any single host with a delay between them, and `robots.txt` fetched once per
host and cached. A crawler that hammers one origin gets the IP blocked, and this
machine's IP is already on a blocklist.

## Files

| File | Role |
| --- | --- |
| `db.py` | Schema, upserts, domain cleaning. The only place a confidence is assigned. |
| `discover.py` | YC directory + job boards → companies and founder names |
| `hn.py` | HN Who-is-hiring threads → companies |
| `harvest.py` | Company sites → published addresses. The long pole. |
| `run_all.py` | All of the above in order, for an unattended run |

## Usage

```bash
# everything, resumable, safe to kill and rerun
python3 -m crawler.run_all --hn-months 24

# or stage by stage
python3 -m crawler.discover --source directory --with-founders
python3 -m crawler.hn --months 24 --apply
python3 -m crawler.harvest --apply --concurrency 40
python3 score_confidence.py --apply
```

`harvest.py` records `site_scraped_at` per domain as it goes and commits every
25 domains, so an interrupted run resumes instead of restarting. An earlier
version accumulated everything in memory and wrote at the end; three hours in,
an interruption threw away three hours.

Every stage runs read-only without `--apply` (`harvest.py`, `hn.py`) or
`--slugs-only` (`discover.py`). Check the sample output before writing.

## What this does not do

- No LinkedIn. It is against their terms, it gets the account banned, and the
  data is not worth either.
- No search engines and no third-party directories — company domains only.
- No pattern guessing. `exec_finder/email_finder.py` still does that and is
  kept for the demo write-up, but nothing here calls it.

## Known limits

- **Coverage is ~25-30%.** Most company sites publish no address at all, or
  only a form. That is the real ceiling of this approach, not a bug to tune away.
- **Shared mailboxes dominate.** `careers@`, `hello@`, `founders@` are more
  common than a person's address. `local_rank` in `find_real_emails.py` orders
  them; one address per company is kept.
- **A published address is not a verified one.** It is evidence the mailbox was
  real when the page was written. `email_verify.py` is still the next step, and
  is still blind on catch-all and Google-hosted domains.
