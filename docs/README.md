# Cold Mail DB

A cold outreach pipeline I run on my own job search. It verifies an address
before sending, sends through Gmail SMTP under a daily cap, tracks opens and
clicks, then reads my own inbox over IMAP to find bounce reports and retire
addresses that are dead.

Four moving parts:

```
scraper / contact db  ->  verification  ->  sender  ->  tracker
   turso-full.db        email_verify.py  send_emails.py  tracker/app.py
```

## Two tiers

The pipeline above is the volume tier. Sector templates in `templates/`, a
daily cap, cooldowns and locks, and bounce retirement, all so a lot of mail can
go out without wrecking the sending domain.

`outreach/` is the other tier: roughly fifteen dream-shot companies, each one
getting a hand-researched email and a five slide technical deck built around a
specific finding in that company's product or code. It writes
`outreach/out/<slug>/send.json`, and `install_outreach_sends()` merges those
into `PERSONALIZED_EMAILS` at startup so they ride this same sender, cap,
tracker and bounce handling. `build_email` attaches the deck alongside the
resume for those contacts only.

```
outreach/  ->  out/<slug>/send.json  ->  PERSONALIZED_EMAILS + OUTREACH_DECKS
                                          (send_emails.py, unchanged path)
```

Hand written entries in `PERSONALIZED_EMAILS` win on conflict, so a stale build
cannot silently replace something typed deliberately. See `outreach/README.md`.

## Why verification is the interesting part

An SMTP `RCPT TO` probe asks the recipient's mail server whether a mailbox
exists, without sending anything. It is free and unlimited, which matters when
every paid verification API either runs out of credits or requires a business
domain.

It also has one serious blind spot. **Catch-all domains accept `RCPT TO` for
literally any address** and only reject unknown mailboxes later, during real
delivery. So a "valid" result from a catch-all domain means nothing at all.

`is_catchall_domain` handles this by probing a deliberately fake address at the
same domain first. If `definitely-not-real-xyz@company.com` is also accepted,
the real address's result is discarded as untrustworthy.

That leaves three outcomes rather than two, and the third one is the point:

| Result | Meaning | Action |
| --- | --- | --- |
| `True` | Mailbox confirmed | Send |
| `False` | Mailbox rejected | Discard the contact |
| `None` | Cannot be determined | **Hold back. Do not send, do not delete.** |

`None` happens when SMTP is blocked, the probe is inconclusive, or the domain is
catch-all. Collapsing it into either `True` or `False` is the tempting shortcut
and both directions are costly: guessing `True` sends into a mailbox that may
bounce, and repeated bounces are what gets a sending domain flagged. Guessing
`False` throws away a real lead permanently. Refusing to answer is the correct
behaviour when you genuinely do not know.

## Bounce handling

Bounces do not arrive as an API callback. They arrive as email, from
`mailer-daemon` or `postmaster`, and the machine-readable part is a
`message/delivery-status` MIME section (a DSN).

`check_and_sync_bounces` connects over IMAP, searches only messages newer than
the last processed UID, walks each message for its DSN part, and reads the
status and diagnostic codes to classify the failure:

- **hard** — the mailbox does not exist. Retire the address.
- **soft** — temporary, for example a full mailbox or rate limiting. Retry later.
- **unknown** — a bounce that could not be parsed confidently. Treated as
  soft, because deleting a good contact is worse than one wasted retry.

The last UID is persisted to disk, so a rerun does not rescan the whole inbox
and does not double count a bounce against a daily quota.

## Tracking

`tracker/app.py` is a small Flask service holding sends, opens, and clicks in
SQLite on a persistent disk.

- `/t/<encoded>.gif` returns a 1x1 pixel and records an open
- `/c/<encoded>.<sig>` records a click and redirects to the real destination

Both are deliberately unauthenticated, because the client fetching them is the
recipient's mail client and it will never have a key. Everything else, including
the dashboard and every write endpoint, requires `TRACKER_SECRET`.

That has two consequences worth being explicit about.

**Neither endpoint may create rows.** They record against a recipient that
already exists in `sends` and ignore anything else. Otherwise a crafted
`/t/<base64>.gif` would let anyone append to the sends table, and `total_sent`
is the denominator of both the open rate and the bounce rate.

**The click payload carries its own redirect target**, so it is signed with a
truncated HMAC over the encoded payload, keyed on `TRACKER_SECRET`. Without
that, `/c/` is an open redirect: anyone could base64 their own destination and
borrow the tracker's hostname to point at it. The sender refuses to wrap links
at all when no secret is configured — no click tracking beats an unsigned one.

Open tracking lies more than people admit. Apple Mail Privacy Protection
preloads remote images, and corporate scanners like `OutlookSafeLinksScanner`
and `Google-Safety` pre-fetch links, both of which look identical to a human
opening your email. Known scanner user agents are classified as bots and the
dashboard reports confirmed and unconfirmed counts separately rather than
presenting one inflated number.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

The sender and verifier need one third-party package (`dnspython`). Everything
else they use is standard library. The tracker service and the scraper have
their own requirements files, `tracker/requirements.txt` and
`requirements_scraper.txt`.

`.env` (gitignored, never commit it):

```
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=<app password, not your login password>
TRACKER_URL=https://your-tracker.onrender.com
TRACKER_SECRET=<same value set on the tracker service>
```

The Gmail app password requires 2FA and comes from
https://myaccount.google.com/apppasswords.

```bash
python3 send_emails.py --dry-run   # resolve and render, send nothing
python3 send_emails.py             # send, up to CONFIG["daily_limit"]
```

The tracker deploys separately from `render.yaml`. Set `TRACKER_SECRET` in the
service environment to the same value as your local `.env`, or every write from
the sender returns 401 and the dashboard stays locked.

`TRACKER_SECRET` is not optional and is not just for the dashboard. It also keys
the click-link HMAC, so the two sides must hold the identical value: a mismatch
means every click redirect is rejected as forged. If the variable is missing on
the server the gated routes return 503 rather than 401, which is the quickest
way to tell "never configured" apart from "configured, wrong key".

## Files

| File | Role |
| --- | --- |
| `send_emails.py` | Sender, templating, quota, bounce sync |
| `email_verify.py` | MX check, SMTP probe, catch-all detection |
| `bounce_scan.py` | Standalone bounce sweep |
| `check_verify_services.py` | Availability check for verification providers |
| `tracker/app.py` | Flask tracker and dashboard |
| `template.txt` | Default email body |
| `templates/<sector>.txt` | Sector-specific bodies for the volume tier |
| `outreach/` | Tier one: per-company email and technical deck |

## Limitations

- Gmail SMTP, so throughput is bounded by Google's sending limits. This is a
  personal outreach tool and does not try to be a sending platform.
- SMTP probes are refused outright by some providers, which is exactly the
  `None` case above.
- Open rates are estimates. See the tracking section.
- No tests.
