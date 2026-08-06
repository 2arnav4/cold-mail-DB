#!/usr/bin/env python3
"""build_batch_emails.py -- write a per-company email for each contact in the batch.

Output goes to `outreach/out/<slug>/send.json`, which is the path
`send_emails.py:install_outreach_sends()` already reads at startup. No sender
changes are needed: these ride the same daily cap, cooldowns, tracker and bounce
handling as everything else, and hand-written entries in PERSONALIZED_EMAILS
still win on conflict.

Shape of each email, and the reasoning behind it:

  - **Plain text.** Delivered as the sender's existing plain+HTML alternative,
    but with no styling of its own. A cold note from a personal Gmail that looks
    like a designed campaign is filtered like one.
  - **One researched opening line per company**, written from that company's own
    homepage and YC listing. This is the only part that varies, and it is the
    only part that makes the email not a mailmerge.
  - **Resume as a link, not an attachment.** arnav24.tech/resume.pdf returns a
    real PDF; an attachment from an unknown sender raises the spam score.
  - **No em-dashes**, per the writing style this is drafted in.

Usage:
  python3 build_batch_emails.py            # write the files
  python3 build_batch_emails.py --preview  # print them, write nothing
"""
import argparse
import json
import os
import re
import sqlite3
import sys

# This script lives one directory below the repo root, but the database, the
# sent logs and the shared modules all sit at the root. Resolve it explicitly
# rather than relying on the working directory, so the script behaves the same
# from cron, from an editor, or from anywhere on the filesystem.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from next_batch import build  # noqa: E402

OUT = os.path.join(HERE, "outreach", "out")

# Companies to skip regardless of ranking, with the reason. Reforged Labs' own
# homepage reads "Reforged Labs has closed" -- mailing a shut company wastes a
# slot and looks careless if anyone forwards it.
SKIP = {"reforgedlabs.com": "company has shut down (stated on their homepage)"}

SIGNOFF = """
Resources:
• Portfolio: https://arnav24.tech
• Resume: https://arnav24.tech/resume.pdf
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3

Best,
Arnav Singla"""

BODY_MIDDLE = """
I'm Arnav, a third year CSE student at ADGIPS in Delhi, graduating 2028. I work
across the stack: React and Next.js with TypeScript on the front, Node/Express
and Go behind it, Postgres and Mongo underneath.
"""

# Every project carries its live link: naming one without a URL leaves the
# reader nothing to check, so the claim does no work. Companies pick from these
# by tag rather than receiving all three -- an identical project list under
# every opening line is the mailmerge shape the personalisation exists to avoid.
PROJECTS = {
    "pulse": ("Pulse", "https://pulse-nu-liard.vercel.app/",
              "a collaborative workspace handling 3,000+ tasks across teams,\n  with Groq-generated standups and a JWT/Helmet secured backend"),
    "lucent": ("Lucent", "https://lucent-fintech-psi.vercel.app/",
               "a finance dashboard tracking 200+ stocks and crypto in real\n  time through Finnhub, MarketStack and CoinMarketCap"),
    "student": ("Student Helper", "https://student-helper-yaye.vercel.app/",
                "used by 150+ students, with notes sharing, a StudySwap\n  marketplace and role based access"),
}


def projects_block(keys) -> str:
    """Render only the named projects, each with its live link."""
    picked = [PROJECTS[k] for k in keys if k in PROJECTS]
    if not picked:
        picked = [PROJECTS["pulse"]]
    lead = ("Something I've built that is close to this:" if len(picked) == 1
            else "Two things I've built and can walk you through line by line:")
    bullet = "\u2022 "
    lines = [bullet + f"{name} ({url}), {desc}" for name, url, desc in picked]
    return lead + "\n" + "\n".join(lines)


# (subject, opening line, closing line) per address. Written from each company's
# own homepage h1/meta and YC one-liner, which is why every opener names
# something only that company does.
# Keyed by company domain, never by the recipient's address. This file is
# committed to a public repository; a company's domain is public
# information, a named person's work address is not. build_batch_emails
# resolves the actual recipient from the database at run time.
EMAILS = {
"synthiolabs.com": ("Agentic AI for pharma GTM, and a student who likes compliance constraints",
 "Synthio building the intelligence layer for life sciences engagement means shipping into a domain where 'compliant' is a hard requirement rather than a checkbox, which is a more interesting engineering problem than most.",
 "You have six roles open. Any of them have room for an intern?"),
"twenty.com": ("Open source CRM, and someone who reads the repo",
 "Twenty being an open source CRM is the part that caught me. Most CRMs are a black box you rent; yours is one you can actually read and fix.",
 "You're hiring for two roles. Would an intern be useful?"),
"framer.com": ("Design agent, from idea to launch",
 "Framer moving from a design tool to something that carries an idea all the way to a launched site is a much harder product bet than adding features, and the no-code AI builder is where that shows.",
 "You have a role open. Any space for an intern?"),
"sensehawk.com": ("An operating system for solar, from Delhi",
 "Sensehawk running GIS and asset management for utility-scale solar is infrastructure software where a bug costs real megawatts, not just a bad render.",
 "Not sure if you're taking interns right now, but I'd like to be on the list if so."),
"zomentum.com": ("Proposals, renewals and payments in one place",
 "Zomentum unifying proposals, renewals and payments for IT channel partners is the kind of consolidation that sounds boring and is usually where all the hard state management lives.",
 "Would an intern be useful to your team?"),
"ixigo.com": ("Trains, flights and an intern in Delhi",
 "ixigo doing flights, trains and buses at Indian price sensitivity means optimising for a user who will absolutely leave over fifty rupees, which is a brutal and useful constraint.",
 "I'm in Delhi. Any intern-shaped gap on your engineering team?"),
"wearcomet.com": ("Sneakers, and a backend student who shops online",
 "Comet building a homegrown sneaker brand for modern India is a category where the D2C stack has to work as hard as the design does.",
 "Would an intern be useful on the tech side?"),
"haberwater.com": ("AI and IoT on real water systems",
 "Haber putting real-time optimisation and autonomous control on industrial water is a rare case of ML where the feedback loop is physical and immediate.",
 "Any room for an intern on the engineering team?"),
"californiaburrito.in": ("Burritos in Bangalore, and an intern who builds ordering systems",
 "California Burrito running a fast casual chain in Bangalore is an operations problem where the software either keeps up with the kitchen or gets abandoned.",
 "If there's any engineering work I could help with as an intern, I'd like to hear about it."),
"thesouledstore.com": ("D2C at scale, and a full stack student",
 "The Souled Store being India's largest fan merchandise destination means a catalogue and checkout that have to survive a drop-day traffic spike, which is the fun part.",
 "Would an intern be useful to your engineering team?"),
"unifyapps.com": ("Agentic automation for the enterprise",
 "UnifyApps doing agentic automation across departments means the integration surface is the product, and that is usually where the real work hides.",
 "Any space for an intern?"),
"speakx.ai": ("An AI English teacher, and a student who wants to build one",
 "SpeakX giving instant speech feedback in fifteen minute lessons is a latency problem as much as a model problem, and getting that fast enough to feel like a conversation is genuinely hard.",
 "Would an intern be useful?"),
"zeni.ai": ("AI bookkeeping, and someone who built a finance dashboard",
 "Zeni pairing AI bookkeeping with actual accountants is the honest version of this, and closer to something I built badly and learned from: a dashboard reconciling 200+ tickers across three APIs.",
 "Any room for an intern?"),
"adopt.ai": ("Reconciliations that run themselves",
 "Adopt AI running reconciliations and workpapers end to end on the systems people already use, rather than asking them to migrate, is the constraint that makes it hard and makes it adoptable.",
 "Would an intern be useful to you?"),
"sharechat.com": ("Fifteen languages, one social network",
 "ShareChat operating in fifteen Indian languages is a localisation and scale problem most products never have to think about, and I'd like to see how that is actually held together.",
 "I'm a Delhi-based CSE student. Any intern openings on the engineering side?"),
"coval.dev": ("Testing voice agents before they embarrass you",
 "Coval testing voice agents in simulation rather than waiting for a bad live call is the right way round, and it is a genuinely hard evaluation problem.",
 "You have three engineering roles open. Any of them have room for an intern who likes breaking things on purpose?"),
"brainbaselabs.com": ("An intern for the AI agent cloud",
 "Brainbase running agents across any model or harness is the part everyone underestimates, since the harness is usually where things quietly fall apart.",
 "You're hiring two engineers. Would an intern be useful to either?"),
"mineflow.ai": ("Predicting rock, from Delhi",
 "Mineflow predicting the shape and location of mineral deposits is the most physically consequential ML I have read about this month. Most models argue about clicks.",
 "You have an engineering role open. Any room for an intern alongside it?"),
"axle.energy": ("Clearinghouse infrastructure and one keen intern",
 "Axle building an AI native clearinghouse for insurance means the boring plumbing everyone else routes around, which is usually where the real engineering is.",
 "You're hiring. Would an intern be useful?"),
"startraven.com": ("Making P&IDs clickable",
 "Raven turning existing plant P&IDs into something you can click a valve on is a lovely piece of interface work sitting on top of genuinely messy source data.",
 "You have a role open. Any space for an intern?"),
"reworkd.ai": ("End to end extraction, and a student who likes scrapers",
 "Reworkd doing end to end data extraction is a problem I have hit from the other side, building a crawler that reads company sites and keeps getting outsmarted by JavaScript.",
 "Not sure if you're hiring interns right now, but I'd like to be on your list if you are."),
"launchflow.com": ("Modern software for local government",
 "LaunchFlow embedding in teams to rebuild broken government workflows is unglamorous and matters more than most of what gets funded.",
 "Would an intern be useful to you?"),
"repspark.com": ("B2B wholesale, and an intern who reads schemas",
 "RepSpark being the platform brands actually run wholesale ordering on means the data model has to survive contact with a lot of real businesses.",
 "You have an engineering role open. Any room for an intern?"),
"corgi.com": ("49 open roles, one more application",
 "Corgi doing business insurance at the speed of compute, with modular CGL and Tech E&O quoted in minutes, is a rare case of insurance moving at software pace.",
 "You have 49 roles open, so I will assume you are busy. If any of them have an intern-shaped gap next to them, I'd like to hear about it."),
"blacksmith.sh": ("Faster CI, and a student who has waited on plenty of it",
 "Blacksmith making GitHub Actions run 2x faster is the kind of improvement every developer feels immediately and nobody thanks you for.",
 "You're hiring across 44 roles. Is an intern useful to any of them?"),
"flagright.com": ("AML compliance and an intern who likes hard rules",
 "Flagright building an AI native operating system for financial crime compliance means shipping into an environment where a false negative is a regulatory problem, not a bug report.",
 "You have 29 roles open. Room for an intern in any?"),
"clinikally.com": ("Dermatology at Indian internet scale",
 "Clinikally running teleconsults and pharmacy for dermatology across India is a logistics problem wearing a healthcare hat, and I would like to see it up close.",
 "I'm in Delhi and you have 29 roles open. Any of them take an intern?"),
"ultra.tech": ("Robots in warehouses, student in Delhi",
 "Ultra putting general purpose robots into American warehouses for the repetitive work is the version of automation that has to survive real dust and real deadlines.",
 "You're hiring across 24 roles. Any space for an intern?"),
"checkr.com": ("Verification at high stakes",
 "Checkr running background checks as high-stakes infrastructure means accuracy is the product, not a feature you tune later.",
 "You have 23 roles open. Would an intern be useful?"),
"withdavid.ai": ("Audio data, and someone who wants to learn it properly",
 "David AI building proprietary audio datasets that top labs actually train on is upstream of every voice product that works, which is a good place to be.",
 "You're hiring across 19 roles. Room for an intern?"),
"solveintelligence.com": ("AI for patents, from a CS student",
 "Solve Intelligence covering drafting through prosecution and litigation means the model has to be right in a domain where being confidently wrong is expensive.",
 "You have 17 roles open. Any of them intern friendly?"),
"modelml.com": ("A finance agent, and an intern who built a finance dashboard",
 "Model ML being a finance agent wherever you are is close to something I built badly and learned a lot from, a dashboard pulling 200+ live tickers across three APIs.",
 "You have 15 roles open. Would an intern be useful?"),
"harperinsure.com": ("One call, every market",
 "Harper learning a business once and then working every market that can write it is a nice inversion of how brokerage usually wastes people's time.",
 "You're hiring across 14 roles. Any room for an intern?"),
"shopgarage.com": ("A marketplace for fire trucks and an intern",
 "Garage being America's marketplace for specialized vehicles is a category most people never think about until they need one, which usually means the software is decades behind.",
 "You have 14 roles open. Space for an intern?"),
"hellolunajoy.com": ("Mental health infrastructure, and a student who wants in",
 "LunaJoy pairing scalable infrastructure with actual clinicians for women's mental health is the harder of the two ways to build this.",
 "You have 14 roles open. Would an intern be useful?"),
"itsrever.com": ("Returns that don't ruin the week",
 "REVER making ecommerce returns fast for both sides is one of those problems everyone has felt personally and very few have fixed.",
 "You're hiring across 14 roles. Any intern shaped gap?"),
"pump.co": ("The Costco for cloud",
 "Pump cutting cloud bills without asking engineers to do anything is a good trick, and the fact that the pricing page just says FREE CLOUD made me look twice.",
 "You have 14 roles open. Room for an intern?"),
"givechariot.com": ("$326B in DAFs and a backend student",
 "Chariot connecting nonprofits to donor advised funds is a payments problem where the hard part is the plumbing between institutions that do not want to talk to each other.",
 "You're hiring across 14 roles. Would an intern be useful?"),
"usepylon.com": ("Support at lightspeed",
 "Pylon having humans and AI agents work the same customer thread, rather than bolting a bot in front of a queue, is the version of this I would actually want to use.",
 "You have 14 roles open. Any space for an intern?"),
"assemblyai.com": ("Voice AI infrastructure, from a student who builds on APIs like yours",
 "AssemblyAI being the speech layer other people build on is the position I find most interesting, since every mistake shows up in someone else's product.",
 "You're hiring across 12 roles. Room for an intern?"),
"middesk.com": ("Business identity that runs itself",
 "Middesk doing verification, fraud and credit risk for B2B onboarding is a data quality problem dressed as an API, which is my favourite kind.",
 "You have 12 roles open. Would an intern be useful?"),
"dynamo.ai": ("Making AI boring enough for the enterprise",
 "Dynamo AI working on performance, security and compliance together is the unglamorous half of shipping models that nobody demos and everybody needs.",
 "You're hiring across 12 roles. Any of them intern friendly?"),
"pointone.com": ("Passive time tracking for law firms",
 "PointOne capturing time passively instead of asking lawyers to remember is the correct answer to a problem every firm has quietly given up on.",
 "You have 12 roles open. Room for an intern?"),
"mercura.ai": ("Quotes that don't get lost",
 "Mercura automating quote and order handling for distributors is the kind of workflow that still runs on email attachments at most companies.",
 "You're hiring across 11 roles. Would an intern be useful?"),
"hud.ai": ("An intern for HUD",
 "HUD working on agent evaluation infrastructure sits right where I have been spending my own time, trying to tell whether a pipeline is actually correct or just quiet.",
 "You have 11 roles open. Any space for an intern?"),
"escape.tech": ("API security, and a student who has left endpoints open",
 "Escape finding the API surface people forget they exposed is a good business precisely because everyone thinks their own inventory is complete.",
 "You're hiring across 11 roles. Room for an intern?"),
}


def slug(domain: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")


def first_name(name: str | None, email: str) -> str:
    if name and name.strip():
        return name.strip().split()[0]
    return email.split("@", 1)[0].split(".")[0].title()


def compose(d) -> tuple[str, str] | None:
    entry = EMAILS.get(d["domain"])
    if not entry:
        return None
    subject, opener, closer = entry[0], entry[1], entry[2]
    picks = entry[3] if len(entry) > 3 else ("pulse",)
    body = (f"Hi {first_name(d['name'], d['email'])},\n\n"
            f"{opener}\n"
            f"{BODY_MIDDLE}\n"
            f"{projects_block(picks)}\n\n"
            f"{closer}\n"
            f"{SIGNOFF}\n")
    return subject, body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("-n", "--limit", type=int, default=32)
    args = ap.parse_args()

    con = sqlite3.connect(os.path.join(HERE, "turso-full.db"), timeout=60)
    con.row_factory = sqlite3.Row
    batch, _ = build(con, args.limit, None, False)

    written = skipped = 0
    for d in batch:
        if d["domain"] in SKIP:
            print(f"  SKIP {d['email']:<34} {SKIP[d['domain']]}")
            skipped += 1
            continue
        made = compose(d)
        if not made:
            continue
        subject, body = made
        if args.preview:
            print("=" * 78)
            print(f"TO:      {d['email']}   ({d['company']})")
            print(f"SUBJECT: {subject}")
            print("-" * 78)
            print(body)
        else:
            path = os.path.join(OUT, slug(d["domain"]))
            os.makedirs(path, exist_ok=True)
            json.dump({"recipient_email": d["email"], "subject": subject,
                       "body": body, "deck": None},
                      open(os.path.join(path, "send.json"), "w"), indent=1)
        written += 1

    if not args.preview:
        print(f"wrote {written} send.json files under outreach/out/")
        print(f"skipped {skipped}")
        print("\nsend_emails.py picks these up automatically via install_outreach_sends()")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
