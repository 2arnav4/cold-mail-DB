# outreach — tier one

The rest of this repo is a volume system: caps, cooldowns, locks, sector
templates, bounce retirement. All of that exists to send a lot of mail without
wrecking the sending domain.

This is the other tier. Fifteen companies, one artifact each. It builds a
per-company email and a five slide technical deck, then hands both to
`send_emails.py` so tier one rides the same sender, cap, tracker and bounce
handling rather than a second delivery path.

The method comes from an off-campus masterclass aimed at marketing roles,
translated into engineering: the deck is not the work sample, it is a wrapper
around the work sample. Slide 2 is the whole point.

## Install

```
cd outreach
npm install
npx puppeteer browsers install chrome
```

`node:sqlite` reads `../turso-full.db` directly, so there is no extra database
dependency.

## Per company

```
npm run new      -- stripe --from-db stripe.com   scaffold from the contact db
npm run research -- stripe stripe                 pull what they actually write
                                                  (edit the yaml: this is the 40 minutes)
npm run check    -- stripe                        refuse generic output
npm run build    -- stripe                        email.txt + deck.pdf + send.json
npm run log      -- stripe --sent
npm run status
```

`--from-db` fills the company name, description and best recipient from
`turso-full.db`, and prints the other candidates ranked founder and engineering
first, HR last. Pass a plain name instead if the company is not scraped yet.

`check` and `build` with no slug run across every company at once.

## The handoff to send_emails.py

`build` writes `out/<slug>/send.json`:

```json
{ "recipient_email": "...", "subject": "...", "body": "...", "deck": "/abs/path.pdf" }
```

`install_outreach_sends()` merges those into `PERSONALIZED_EMAILS` at startup
and registers the deck in `OUTREACH_DECKS`. `build_email` then attaches the
resume and the deck. Contacts without a deck are untouched.

Hand written entries already in `PERSONALIZED_EMAILS` win on conflict, so a
stale build can never silently replace something typed deliberately.

The resume comes from the repo root (`ARNAV-RESUME.pdf`, set in `CONFIG`).
Nothing needs to go in `outreach/assets/`.

## What is automated and what is not

Automated: contact lookup, GitHub org enrichment, rendering, PDF generation,
the lint that stops you sending something generic.

Not automated, on purpose: reading their engineering blog, their docs, and the
job posting; finding the technical hook; writing the five lines. A company
homepage is marketing copy and there is no angle in it. `research` gives you
the languages they write, what they pushed recently, and their open
good-first-issues. Everything after that is judgement.

## The company file

`companies/<slug>.yaml`. Two fields matter more than the rest:

**`hook`** is the first line of the email and it is about them. A release, a
blog post, an issue, a funding round. It needs a URL, because no URL means you
did not actually verify it.

**`finding`** is the artifact. Without it there is no reason for the email to
exist. Ranked by how strongly it lands:

1. `pr` a merged pull request on their repo. Nothing beats this.
2. `bug` a reproducible bug report with a minimal repro and a proposed fix.
3. `tool` something small you built against their public API, deployed.
4. `teardown` a written technical analysis of their product.

`companies/example-northwind.yaml` is a fully filled in one. Northwind is
fictional, including the recipient. It is the only company file in git;
everything else is gitignored because it carries real contact data, same rule
as `hr-database/`.

## Why check.ts fails the build

A tool that makes it easy to send generic mail is worse than no tool: it turns
fifteen careful sends into fifteen lazy ones. The build refuses while any
required field is still a placeholder, and warns when:

- the copy reads like a mail merge, or the company is never named in the body
- the recipient is `careers@`, or their title is HR or recruiting
- the body runs past 130 words
- the evidence block will clip on slide 2
- the address is not confirmed by `email_verify.py`, because a bounce on a
  dream-shot send costs the domain

It also enforces house style: no em-dashes, no phone number in the footer.

## Framing

The deck says "I noticed X, here is what I tried and what I measured". It does
not say "here is what you are doing wrong". You are a third year student
emailing someone who owns the code. Curiosity lands, critique does not.

Budget 30 to 45 minutes per company for the research and the writing, which is
the part no script removes. Opens diagnose the subject line. Replies are the
metric.
