# Cold Mail DB

Cold outreach pipeline for my own job search: find companies, read the
addresses they publish, grade each address by how it was obtained, and only
mail the ones with real evidence behind them.

```
crawler/  ->  contacts  ->  verify/  ->  send_emails.py  ->  tracker/
 acquire      graded db     probe       sender + quota     opens/clicks
```

## Layout

| path | role |
| --- | --- |
| `send_emails.py` | the sender: templating, quota, cooldowns, bounce sync |
| `email_verify.py` | MX check, SMTP probe, catch-all detection |
| `find_real_emails.py` | address acceptance rules, shared by the crawlers |
| `next_batch.py` | who the next send reaches, and why |
| `crawler/` | acquisition: YC directory, HN, site harvest, Chrome render |
| `verify/` | probing tools, incl. the throttled batch prober |
| `pipeline/` | database maintenance: scoring, dedupe, imports |
| `reports/` | read-only views over the database |
| `sender/` | one-off send and the standalone bounce scanner |
| `tracker/` | Flask service for opens and clicks |
| `outreach/` | tier one: per-company email plus a technical deck |
| `archive/` | superseded code, kept for reference |

The four modules at the root are the ones everything else imports. Scripts in
the folders are standalone entry points; each resolves the repo root from
`__file__` rather than the working directory, so they run identically from
cron, an editor, or anywhere on disk.

## Evidence tiers

`pipeline/score_confidence.py` grades every address by how it was obtained, and
the sender refuses to mail below `CONFIG["min_confidence"]`.

| conf | tier | meaning |
| --- | --- | --- |
| 100 | published | read off the company's own site |
| 95 | researched | found by hand |
| 80 | verified_guess | constructed, but SMTP confirmed the mailbox |
| 75 | list | curated list, real naming scheme |
| 70 | pending_recheck | passed under a broken catch-all check, held |
| 15 | guessed | `firstname@domain`, invented by a pattern generator |

The full story of why this exists, and the three-valued verification it rests
on, is in [docs/README.md](docs/README.md).

## Usage

```bash
source venv/bin/activate

python3 -m crawler.run_all              # acquire
python3 pipeline/score_confidence.py --apply
python3 next_batch.py -n 30             # inspect the queue
python3 send_emails.py --only-personalized --dry-run
python3 send_emails.py --only-personalized --daily-limit 30
python3 reports/db_report.py            # pool composition
```

**Probing is currently paused.** The sending IP is on Spamhaus CSS/XBL/PBL, so
SMTP verdicts from this machine are policy rejections rather than answers.
Resume only through `verify/probe_batch.py` in small batches once it clears.
