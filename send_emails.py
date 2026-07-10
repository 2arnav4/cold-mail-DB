#!/usr/bin/env python3
"""
Cold Mail Sender
----------------
Reads contacts from turso-full.db, sends up to 20 cold emails per day
via Gmail SMTP, tracks sent emails in sent_log.json, and attaches a resume.

Setup:
  1. Fill in CONFIG below (your Gmail, app password, resume path)
  2. Edit template.txt with your email body
  3. Run:  python3 send_emails.py
  4. To do a dry run (no emails sent): python3 send_emails.py --dry-run

Gmail App Password (NOT your regular password):
  https://myaccount.google.com/apppasswords
  (Requires 2FA to be enabled on your Google account)
"""

import sqlite3
import smtplib
import json
import os
import sys
import argparse
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

# ─────────────────────────────────────────────
#  CONFIG — loaded from .env file automatically
# ─────────────────────────────────────────────
import os as _os

def _load_env(path=".env"):
    """Parse a .env file and inject into os.environ (no dependencies needed)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                _os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass  # .env is optional; fall back to real env vars

_load_env()

CONFIG = {
    "your_email":    _os.environ.get("GMAIL_ADDRESS",      ""),
    "app_password":  _os.environ.get("GMAIL_APP_PASSWORD", ""),
    "your_name":     "Arnav Singla",
    "resume_path":   "ARNAV-RESUME.pdf",
    "db_path":       "turso-full.db",
    "template_path": "template.txt",
    "log_path":      "sent_log.json",
    "daily_limit":   20,
    "tracker_url":   _os.environ.get("TRACKER_URL", ""),
}

# ─────────────────────────────────────────────────────────────────────────────
#  PERSONALIZED EMAILS
#  Key   = recipient email address (exact match from the PDF)
#  Value = { "subject": "...", "body": "..." }
#  For any contact NOT in this dict, the generic template.txt is used.
# ─────────────────────────────────────────────────────────────────────────────
PERSONALIZED_EMAILS = {
    "sean@relixir.ai": {
        "subject": "Internship Opportunity – Relixir",
        "body": """I've been following Relixir since the YC batch announcement. The pivot from traditional SEO to Generative Engine Optimization is the right call. As AI-driven search takes share from Google, brands that don't adapt now will be invisible in two years. The autonomous content publishing and GEO-optimized refresh cycle is a sharp product decision.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

That kind of AI-in-product, production-ready thinking is what your engineering team needs. I'd love to spend a summer helping Relixir build the infrastructure that keeps brands visible in an AI-first world.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3
• Pulse (Live): https://pulse-nu-liard.vercel.app
• Pulse (Code): https://github.com/2arnav4/Pulse"""
    },
    "vimal@kalam.in": {
        "subject": "Internship Opportunity – SuperKalam",
        "body": """SuperKalam's approach to UPSC prep - treating it as a GPS navigation problem rather than a content firehose - is the right mental model. AI-driven personalized study paths with daily accountability streaks solve the consistency problem that kills most serious aspirants.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I'm comfortable across your full stack (Next.js, Node, PostgreSQL) and excited to work on the kind of product that genuinely changes outcomes for students.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3
• Student Helper (Live): https://student-helper-yaye.vercel.app
• Student Helper (Code): https://github.com/2arnav4/Student-Helper"""
    },
    "rajiv@opoyi.com": {
        "subject": "Internship Opportunity – Opoyi",
        "body": """Opoyi's core thesis - trusted, personalized news without the misinformation problem of social feeds - is an important one. The product-first editorial approach shows in how the platform is built. The AI/ML-driven curation layer is what makes it genuinely different from a standard news aggregator.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

Your stack (React, Node, Python) is what I work in daily. I'd love to contribute to the product in Delhi/NCR.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3
• Pulse (Live): https://pulse-nu-liard.vercel.app"""
    },
    "nitish@paasa.co": {
        "subject": "Internship Opportunity – Paasa",
        "body": """Paasa's goal of giving Indian HNIs a Zerodha-equivalent experience for global equities - with IBKR custody, automated compliance, and RSU diversification built-in - is a product gap that's been sitting open for a long time. The YC S24 backing validates the timing.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

Building financial interfaces that users trust requires getting both data accuracy and UX right. I'd love to spend a summer helping Paasa deliver that.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3
• Lucent FinTech (Live): https://lucent-fintech-psi.vercel.app
• Lucent FinTech (Code): https://github.com/2arnav4/Lucent-Fintech"""
    },
    "gaurav@trytejas.ai": {
        "subject": "Internship Opportunity – Tejas AI",
        "body": """Tejas AI's focus on AI-powered credit policy automation for banks is a meaty engineering problem. Turning months-long credit-rule update cycles into a fast, data-driven workflow that reduces default rates is exactly the kind of platform-level work that compounds. The YC W25 backing is well-deserved.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

AI agents that help financial institutions make faster, more reliable decisions need both robust backends and clean interfaces. I'd love to contribute to that at Tejas.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3
• Lucent FinTech (Live): https://lucent-fintech-psi.vercel.app
• Lucent FinTech (Code): https://github.com/2arnav4/Lucent-Fintech"""
    },
    "fyoraaipvtltd@gmail.com": {
        "subject": "Internship Opportunity – Fyora AI",
        "body": """Fyora AI's direction in autonomous AI agents - handling multi-step workflow orchestration, real-time monitoring, and data aggregation - is where serious enterprise automation is headed. The in-office, product-first environment in New Delhi is exactly the kind of setup I'm looking for.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I built Pulse - a collaboration platform handling 3K+ tasks across teams, with Groq AI-powered standup generation. The backend is Node/Express with PostgreSQL (14 REST endpoints, JWT auth, rate limiting) and the frontend is React with reusable component architecture. I'm also experienced with MongoDB from building Student Helper.

Your stack - React, Next.js, Django, MongoDB - maps closely to my day-to-day. I'd be excited to contribute to Fyora AI's roadmap.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3
• Pulse (Live): https://pulse-nu-liard.vercel.app"""
    },
    "careers@uipath.com": {
        "subject": "Internship Opportunity – UiPath",
        "body": """UiPath's bet on agentic automation - combining RPA with AI orchestration to handle exception-heavy, unstructured workflows - is the right next step for enterprise automation. The transition from recording UI interactions to reasoning about multi-step processes is a meaningful technical leap.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

Scalable automation that handles real-world complexity requires clean architecture and reliable backend services. I'd love to contribute to that engineering challenge at UiPath.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@kenko.health": {
        "subject": "Internship Opportunity – Kenko Health",
        "body": """Kenko's mission to make health insurance radically more accessible and actually useful - with instant claims, no TPA friction, and wellness incentives baked in - is fixing one of the most broken consumer experiences in India.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I understand the importance of building products where users need to trust every interaction - especially around health data, access, and thinking through the nuances of healthcare workflows.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@pitchline.com": {
        "subject": "Internship Opportunity – Pitchline",
        "body": """Pitchline's focus on democratizing better sales pitches caught my attention. Using AI to help founders and salespeople communicate better is a high-impact problem.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I built Pulse, which handles 3K+ tasks across teams and integrates Groq AI for real-time standup generation. Wiring up AI APIs, managing async operations, and delivering results cleanly is core to what Pitchline does.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@dpdzero.com": {
        "subject": "Internship Opportunity – DPDZero",
        "body": """DPDZero's focus on data infrastructure for analytics caught my attention. Building systems that process and visualize data reliably is technically fascinating and business-critical.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I built Lucent FinTech - a finance dashboard tracking 200+ stocks in real time via Finnhub and MarketStack, with custom visualizations and optimized caching. The kind of data handling and UI complexity that data platforms demand.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@carboncrunch.com": {
        "subject": "Internship Opportunity – Carbon Crunch",
        "body": """Carbon Crunch's mission to tackle climate challenges resonated. Building technology for sustainability is exactly the kind of high-impact work I want to be part of.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I built Pulse - a collaboration platform with real-time data management and responsive UI. The same architectural thinking applies to climate tech where data accuracy and user engagement drive real-world impact.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@reducate.ai": {
        "subject": "Internship Opportunity – Reducate.ai",
        "body": """Reducate.ai's focus on AI-powered learning immediately clicked. Building personalized education experiences at scale is a problem I care deeply about.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I built Student Helper - a MERN platform serving 150+ students with notes sharing, a writer marketplace, and engagement workflows. The same product thinking applies to Reducate's mission.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@solvusai.com": {
        "subject": "Internship Opportunity – SolvusAI",
        "body": """SolvusAI's focus on GenAI solutions caught my attention. Building AI-powered automation that solves real business problems is exactly the kind of engineering I'm passionate about.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I built Pulse, which handles 3K+ tasks across teams and integrates Groq AI for intelligent standup generation. Understanding how to architect AI features end-to-end, from prompt engineering to UI presentation, is core to my expertise.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@biztel.ai": {
        "subject": "Internship Opportunity – Biztel.AI",
        "body": """Biztel.AI's mission to automate business workflows using AI agents resonated. Building systems that intelligently handle repetitive business tasks is compelling technically and impactful business-wise.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I built Pulse - a platform managing 3K+ tasks across teams with AI-generated standup summaries and role-based workflows. The same end-to-end thinking applies to business automation.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@hunchbite.com": {
        "subject": "Internship Opportunity – Hunchbite",
        "body": """Hunchbite's "production-grade in 14 days" studio model - fixed-price, end-to-end ownership, fast MVPs for startups - is a high-discipline way to run a dev shop. That kind of velocity requires developers who can context-switch quickly, write clean code under time pressure, and own features without hand-holding.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I'd love to contribute to Hunchbite's studio and grow fast by shipping real products for real clients.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
    "careers@softsensor.ai": {
        "subject": "Internship Opportunity – SoftSensor AI",
        "body": """SoftSensor AI's focus on full-stack AI and ML solutions aligned with my growth direction. I'm actively building expertise across data handling, ML integration, and shipping complete systems end-to-end.

I'm Arnav Singla, a third-year B.Tech CSE student at ADGIPS GGSIPU (graduating July 2028). I work across the MERN stack — React with TypeScript and Next.js on the front end, Node.js and Express on the back end, PostgreSQL and MongoDB for data. I also write Go and contribute to open-source across JavaScript, TypeScript, and Go ecosystems.

I built Lucent FinTech, which tracks 200+ stocks and crypto assets in real time and integrates Gemini AI for financial insights. Combining full-stack development with AI integration is where I'm headed.

Resources:
• Portfolio: https://arnav24.tech
• GitHub: https://github.com/2arnav4
• LinkedIn: https://linkedin.com/in/arnav-singla-5683432a3"""
    },
}

# ─────────────────────────────────────────────
#  Contact filter — adjust to target who you want
# ─────────────────────────────────────────────
CONTACT_QUERY = """
    SELECT
        ct.id        AS contact_id,
        ct.name      AS contact_name,
        ct.role      AS contact_role,
        ct.email     AS contact_email,
        co.name      AS company_name,
        co.domain    AS company_domain,
        co.industry  AS company_industry,
        co.funding_stage AS funding_stage
    FROM contacts ct
    JOIN companies co ON ct.company_id = co.id
    WHERE
        ct.email IS NOT NULL
        AND ct.is_invalid = 0
        AND ct.email != ''
    ORDER BY ct.priority DESC, ct.id ASC
"""

# ─────────────────────────────────────────────────────────────────────────────


def load_log(log_path: str) -> dict:
    """Load the sent log.
    Structure: {
      'sent': [...emails...],
      'daily': {'YYYY-MM-DD': count},
      'details': {'email': {'company': ..., 'sent_at': ...}}
    }"""
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            data = json.load(f)
            # Ensure 'details' key exists for older logs
            if "details" not in data:
                data["details"] = {}
            return data
    return {"sent": [], "daily": {}, "details": {}}


def save_log(log_path: str, log: dict):
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)


def load_failed_log(log_path: str) -> list:
    """Load the failed sends log — a list of {email, company, error, timestamp} dicts."""
    failed_path = log_path.replace(".json", "_failed.json")
    if os.path.exists(failed_path):
        with open(failed_path, "r") as f:
            return json.load(f)
    return []


def record_failed(log_path: str, contact: dict, error: str):
    """Append a failed send to failed_log.json."""
    from datetime import datetime
    failed_path = log_path.replace(".json", "_failed.json")
    failed = load_failed_log(log_path)
    failed.append({
        "email":   contact["contact_email"],
        "name":    contact.get("contact_name") or "",
        "company": contact.get("company_name") or "",
        "error":   str(error),
        "time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    with open(failed_path, "w") as f:
        json.dump(failed, f, indent=2)


def already_sent(log: dict, email: str) -> bool:
    return email in log["sent"]


def sent_today(log: dict) -> int:
    today = str(date.today())
    return log["daily"].get(today, 0)


def record_sent(log: dict, email: str, company: str = ""):
    """Record a successful send — email list, daily count, and per-email details."""
    from datetime import datetime
    today = str(date.today())
    log["sent"].append(email)
    log["daily"][today] = log["daily"].get(today, 0) + 1
    log["details"][email] = {
        "company": company,
        "sent_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    }


def sync_tracker_from_logs(cfg: dict):
    """Re-sync all local sent/bounce/open logs to the Render tracker.
    Also downloads new opens from the tracker to back them up locally,
    making the dashboard fully persistent across Render restarts."""
    tracker_url = cfg.get("tracker_url", "").rstrip("/")
    if not tracker_url:
        return

    import urllib.request, json as _json

    log    = load_log(cfg["log_path"])
    failed = load_failed_log(cfg["log_path"])

    # Load local opens backup
    opens_path = cfg["log_path"].replace(".json", "_opens.json")
    local_opens = []
    if os.path.exists(opens_path):
        try:
            with open(opens_path, "r") as f:
                local_opens = _json.load(f)
        except Exception:
            pass

    # 1. Download current opens from Render to back them up locally
    try:
        req = urllib.request.Request(f"{tracker_url}/api/stats", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            server_opens = _json.loads(resp.read())
        
        # Merge server opens into local backup (deduplicate by (email, opened_at))
        local_keys = {(o["email"], o["opened_at"]) for o in local_opens}
        added_new = False
        for o in server_opens:
            key = (o["email"], o["opened_at"])
            if key not in local_keys:
                local_opens.append(o)
                local_keys.add(key)
                added_new = True
        
        if added_new:
            # Sort by opened_at descending
            local_opens.sort(key=lambda x: x.get("opened_at", ""), reverse=True)
            with open(opens_path, "w") as f:
                _json.dump(local_opens, f, indent=2)
    except Exception as err:
        print(f"  Warning: Could not fetch opens backup from server: {err}")

    # Build sends payload from the details dict (has company + sent_at)
    sends = [
        {"email": email, "company": info.get("company", ""), "sent_at": info.get("sent_at", "")}
        for email, info in log.get("details", {}).items()
    ]
    # Fall back: emails in sent[] with no details entry get a bare record
    details_emails = set(log.get("details", {}).keys())
    for email in log.get("sent", []):
        if email not in details_emails:
            sends.append({"email": email, "company": "", "sent_at": ""})

    bounces = [
        {"email": b["email"], "company": b.get("company", ""),
         "reason": b.get("error", "Bounce — invalid address"), "sent_at": b.get("time", "")}
        for b in failed
    ]

    try:
        payload = _json.dumps({"sends": sends, "bounces": bounces, "opens": local_opens}).encode()
        req = urllib.request.Request(
            f"{tracker_url}/api/bulk_sync",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        print(f"  Tracker synced — {result.get('synced_sends', 0)} sends, {result.get('synced_bounces', 0)} bounces, {result.get('synced_opens', 0)} opens")
    except Exception as e:
        print(f"  Tracker sync warning: {e}")


def load_template(template_path: str) -> tuple:
    """
    Returns (subject, body). Template format:
      First line: Subject: <subject text>
      Blank line
      Rest: body
    """
    with open(template_path, "r") as f:
        content = f.read()

    lines = content.splitlines()
    subject = ""
    body_start = 0

    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line[len("subject:"):].strip()
            body_start = i + 1
            break

    # Skip blank lines after subject
    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    body = "\n".join(lines[body_start:])
    return subject, body


def render(template: str, contact: dict) -> str:
    """Replace {{placeholders}} with contact data."""
    result = template
    replacements = {
        "{{contact_name}}":   contact.get("contact_name") or "there",
        "{{first_name}}":     (contact.get("contact_name") or "there").split()[0],
        "{{company}}":        contact.get("company_name") or "",
        "{{company_domain}}": contact.get("company_domain") or "",
        "{{role}}":           contact.get("contact_role") or "",
        "{{industry}}":       contact.get("company_industry") or "",
        "{{funding_stage}}":  contact.get("funding_stage") or "",
    }
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result

import re

def text_to_html(text: str, pixel_tag: str = "") -> str:
    """Convert plain text email body to clean HTML with clickable links."""
    import html as html_lib

    # Escape HTML special chars first
    escaped = html_lib.escape(text)

    # 1. Parse markdown links [Link Text](URL)
    escaped = re.sub(
        r'\[([^\]]+)\]\((https?://[^)]+)\)',
        r'<a href="\2">\1</a>',
        escaped
    )

    # 2. Auto-link remaining raw https:// and http:// URLs (ignoring already linked ones)
    escaped = re.sub(
        r'(?<!href=")(?<!">)(https?://[^\s<>"]+)',
        r'<a href="\1">\1</a>',
        escaped
    )

    # Convert newlines to <br> and wrap in clean HTML
    body_html = escaped.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #222; max-width: 600px;">
{body_html}
{pixel_tag}
</body>
</html>"""

def build_email(cfg: dict, contact: dict, subject: str, body: str) -> MIMEMultipart:
    # Use personalized email if we have one for this exact address
    personalized = PERSONALIZED_EMAILS.get(contact["contact_email"])
    if personalized:
        final_subject = personalized["subject"]
        final_body    = personalized["body"]
    else:
        final_subject = render(subject, contact)
        final_body    = render(body, contact)

    # Build tracking pixel tag if TRACKER_URL is configured
    tracker_url = cfg.get("tracker_url", "").rstrip("/")
    pixel_tag = ""
    if tracker_url:
        import base64 as _b64
        payload  = f"{contact['contact_email']}|{contact.get('company_name', '')}"
        encoded  = _b64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        pixel_tag = f'<img src="{tracker_url}/t/{encoded}.gif" width="1" height="1" style="display:none;" />'

    # Send as multipart/alternative (plain text + HTML) so links are clickable
    msg = MIMEMultipart("mixed")  # outer container (holds alternative + attachment)
    msg["From"]    = f"{cfg['your_name']} <{cfg['your_email']}>"
    msg["To"]      = contact["contact_email"]
    msg["Subject"] = final_subject

    # Inner multipart/alternative for plain + HTML
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(final_body, "plain", "utf-8"))
    alt.attach(MIMEText(text_to_html(final_body, pixel_tag), "html", "utf-8"))
    msg.attach(alt)

    # Attach resume if it exists
    resume_path = cfg["resume_path"]
    if os.path.exists(resume_path):
        with open(resume_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        filename = os.path.basename(resume_path)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)
    else:
        print(f"  WARNING: Resume not found at '{resume_path}' — sending without attachment")

    return msg


def fetch_contacts(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(CONTACT_QUERY)
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def print_summary(sent: list, skipped: int, remaining_today: int, dry_run: bool):
    mode = "[DRY RUN] " if dry_run else ""
    print(f"\n{'─'*50}")
    print(f"  {mode}Done!")
    print(f"  Emails sent this run : {len(sent)}")
    print(f"  Skipped (already sent): {skipped}")
    print(f"  Remaining quota today : {remaining_today}")
    if sent:
        print(f"\n  Sent to:")
        for s in sent:
            print(f"    - {s['contact_email']}  ({s['company_name']})")
    print(f"{'─'*50}\n")


def check_and_sync_bounces(cfg: dict) -> list:
    """Connects to Gmail via IMAP, detects bounces, classifies them as hard (5xx) or
    soft (4xx / temporary), records retry info, and syncs to the Render tracker."""
    import imaplib
    import email as _email
    import re
    import urllib.request as _urllib
    import json as _json
    from datetime import datetime

    print("Checking Gmail for new bounces via IMAP...")
    email_addr  = cfg["your_email"]
    app_pwd     = cfg["app_password"].replace(" ", "")
    tracker_url = cfg.get("tracker_url", "").rstrip("/")

    # Patterns that indicate a HARD bounce (address doesn't exist — no retry)
    HARD_PATTERNS = [
        r"user unknown", r"no such user", r"address rejected",
        r"does not exist", r"invalid address", r"mailbox not found",
        r"user not found", r"account has been disabled", r"no mailbox here",
        r"550", r"551", r"552", r"553", r"554",
    ]
    # Patterns that indicate a SOFT bounce (temporary — Gmail will retry)
    SOFT_PATTERNS = [
        r"mailbox full", r"over quota", r"temporarily unavailable",
        r"service unavailable", r"try again later", r"connection timeout",
        r"452", r"421", r"450", r"451",
    ]
    # Retry delay keywords
    RETRY_PATTERNS = [
        (r"retry.*?(\d+)\s*hour",  "{n}h"),
        (r"(\d+)\s*hour.*?retry",  "{n}h"),
        (r"retry.*?(\d+)\s*day",   "{n} day"),
        (r"will retry for (\d+)",  "~{n}h"),
        (r"try again in (\d+)",    "{n}h"),
    ]

    bounced_records = []   # list of {email, reason, bounce_type, retry_after}

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_addr, app_pwd)
        mail.select("INBOX")

        status, messages = mail.search(
            None,
            '(FROM "mailer-daemon@googlemail.com" SUBJECT "Delivery Status Notification")'
        )
        if status != "OK" or not messages[0].split():
            print("  No bounce notification messages found.")
            mail.logout()
            return []

        message_ids = messages[0].split()
        print(f"  Scanning {len(message_ids)} bounce notification emails...")

        for msg_id in message_ids[-50:]:
            res, msg_data = mail.fetch(msg_id, "(RFC822)")
            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue
                original_msg = _email.message_from_bytes(response_part[1])
                body = ""
                if original_msg.is_multipart():
                    for part in original_msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode(errors="ignore")
                else:
                    body = original_msg.get_payload(decode=True).decode(errors="ignore")

                body_lower = body.lower()

                # Extract bounced address
                matches = re.findall(
                    r"(?:Failed recipient|To|Final-Recipient.*?rfc822|Your message wasn't delivered to)\s*[:\;]?\s*\<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\>?",
                    body, re.IGNORECASE
                )

                for m in matches:
                    if m.lower() == email_addr.lower():
                        continue
                    if any(r["email"] == m for r in bounced_records):
                        continue

                    # Classify bounce type
                    is_hard = any(re.search(p, body_lower) for p in HARD_PATTERNS)
                    is_soft = any(re.search(p, body_lower) for p in SOFT_PATTERNS)
                    bounce_type = "soft" if (is_soft and not is_hard) else "hard"

                    # Build reason string
                    if bounce_type == "hard":
                        reason = "Hard bounce — address not found / rejected (5xx)"
                    else:
                        reason = "Soft bounce — temporary delivery failure (4xx)"

                    # Try to extract retry delay
                    retry_after = ""
                    for pattern, fmt in RETRY_PATTERNS:
                        m2 = re.search(pattern, body_lower)
                        if m2:
                            retry_after = fmt.replace("{n}", m2.group(1))
                            break
                    # Gmail default retry schedule hint
                    if bounce_type == "soft" and not retry_after:
                        retry_after = "24h / 48h / 72h (Gmail auto-retry)"

                    bounced_records.append({
                        "email":       m,
                        "reason":      reason,
                        "bounce_type": bounce_type,
                        "retry_after": retry_after,
                    })

        mail.close()
        mail.logout()
    except Exception as e:
        print(f"  Warning: IMAP bounce check failed: {e}")
        return []

    if not bounced_records:
        print("  No bounced addresses detected.")
        return []

    bounced_emails = [r["email"] for r in bounced_records]
    hard = sum(1 for r in bounced_records if r["bounce_type"] == "hard")
    soft = sum(1 for r in bounced_records if r["bounce_type"] == "soft")
    print(f"  Detected {len(bounced_records)} bounces — {hard} hard, {soft} soft: {bounced_emails}")

    log    = load_log(cfg["log_path"])
    failed = load_failed_log(cfg["log_path"])
    existing_failed = {item["email"] for item in failed}

    new_bounces_logged = 0

    for rec in bounced_records:
        email = rec["email"]
        if email not in log["sent"]:
            log["sent"].append(email)
            today = date.today().strftime("%Y-%m-%d")
            if today in log["daily"]:
                log["daily"][today] = max(0, log["daily"][today] - 1)

        if email not in existing_failed:
            failed.append({
                "email":       email,
                "name":        "",
                "company":     log.get("details", {}).get(email, {}).get("company", ""),
                "error":       rec["reason"],
                "bounce_type": rec["bounce_type"],
                "retry_after": rec["retry_after"],
                "time":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            new_bounces_logged += 1

            if tracker_url:
                try:
                    payload = _json.dumps({
                        "email":       email,
                        "reason":      rec["reason"],
                        "bounce_type": rec["bounce_type"],
                        "retry_after": rec["retry_after"],
                    }).encode("utf-8")
                    req = _urllib.Request(
                        f"{tracker_url}/api/log_bounce", data=payload,
                        headers={"Content-Type": "application/json"}, method="POST"
                    )
                    with _urllib.urlopen(req, timeout=8):
                        pass
                except Exception as te:
                    print(f"    Tracker log_bounce failed for {email}: {te}")

    save_log(cfg["log_path"], log)
    failed_path = cfg["log_path"].replace(".json", "_failed.json")
    with open(failed_path, "w") as f:
        _json.dump(failed, f, indent=2)

    # Clean local SQLite database
    try:
        # Only remove hard bounces from DB — soft bounces may still be valid
        hard_emails = [r["email"] for r in bounced_records if r["bounce_type"] == "hard"]
        if hard_emails:
            conn = sqlite3.connect(cfg["db_path"])
            cur  = conn.cursor()
            cur.executemany("DELETE FROM contacts WHERE email = ?", [(e,) for e in hard_emails])
            conn.commit()
            deleted_count = conn.total_changes
            conn.close()
            print(f"  Removed {deleted_count} hard-bounced contacts from local database.")
    except Exception as dbe:
        print(f"  Warning: Database cleanup failed: {dbe}")

    print(f"  Successfully synced {new_bounces_logged} new bounces.")
    return bounced_emails



def main():
    parser = argparse.ArgumentParser(description="Cold Mail Sender")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print emails without actually sending them")
    parser.add_argument("--limit", type=int,
                        help="Override daily limit for this run")
    parser.add_argument("--show-queue", action="store_true",
                        help="Show the next contacts that would be emailed and exit")
    parser.add_argument("--check-bounces", action="store_true",
                        help="Check Gmail for bounced emails, sync them to logs, clean DB, and exit")
    args = parser.parse_args()
    dry_run = args.dry_run
    cfg = CONFIG.copy()

    # Handle manual bounce check flag
    if args.check_bounces:
        check_and_sync_bounces(cfg)
        sys.exit(0)

    # Validate config
    if not dry_run:
        if cfg["your_email"] == "you@gmail.com":
            print("ERROR: Please set your_email in CONFIG before running.")
            sys.exit(1)
        if cfg["app_password"] == "xxxx xxxx xxxx xxxx":
            print("ERROR: Please set your Gmail app_password in CONFIG before running.")
            sys.exit(1)

    # Automatically check for bounces on startup (if not a dry run)
    if not dry_run:
        check_and_sync_bounces(cfg)
        # Re-sync all local data to Render so dashboard survives server restarts
        sync_tracker_from_logs(cfg)

    # Load state
    log = load_log(cfg["log_path"])
    already_sent_today = sent_today(log)
    quota_left = cfg["daily_limit"] - already_sent_today

    # --limit caps how many we send THIS run (not the daily total)
    if args.limit:
        quota_left = min(quota_left, args.limit)

    print(f"\nCold Mail Sender")
    print(f"  Today's quota: {already_sent_today}/{cfg['daily_limit']} used  ->  {quota_left} left")

    if quota_left <= 0:
        print("  Daily limit already reached. Come back tomorrow!")
        sys.exit(0)

    # Load template
    if not os.path.exists(cfg["template_path"]):
        print(f"ERROR: Template file not found: {cfg['template_path']}")
        sys.exit(1)
    subject_template, body_template = load_template(cfg["template_path"])
    if not subject_template:
        print("ERROR: Template missing 'Subject:' line on the first line.")
        sys.exit(1)

    # Fetch contacts
    all_contacts = fetch_contacts(cfg["db_path"])
    print(f"  Total contacts in DB: {len(all_contacts)}")

    # Filter out already-sent
    queue = [c for c in all_contacts if not already_sent(log, c["contact_email"])]
    print(f"  Unsent contacts     : {len(queue)}")

    if args.show_queue:
        print(f"\n  Next {min(quota_left, 20)} in queue:")
        for c in queue[:20]:
            print(f"    {c['contact_email']:40s} | {c['company_name']} | {c['contact_role']}")
        sys.exit(0)

    # Take only what we're allowed today
    batch = queue[:quota_left]
    skipped_count = len(all_contacts) - len(queue)

    if not batch:
        print("  All contacts have been emailed!")
        sys.exit(0)

    # Send
    sent_this_run = []
    smtp_conn = None

    if not dry_run:
        try:
            smtp_conn = smtplib.SMTP_SSL("smtp.gmail.com", 465)
            smtp_conn.login(cfg["your_email"], cfg["app_password"].replace(" ", ""))
            print(f"  Gmail SMTP connected\n")
        except Exception as e:
            print(f"ERROR: Gmail login failed: {e}")
            print("  Make sure you're using an App Password, not your regular password.")
            print("  Generate one at: https://myaccount.google.com/apppasswords")
            sys.exit(1)

    try:
        for i, contact in enumerate(batch, 1):
            email_addr = contact["contact_email"]
            company    = contact["company_name"]
            role       = contact["contact_role"] or "—"

            print(f"  [{i:02d}/{len(batch)}] {email_addr:40s} | {company} | {role}")

            if dry_run:
                personalized = PERSONALIZED_EMAILS.get(email_addr)
                if personalized:
                    subject = personalized["subject"]
                    body    = personalized["body"]
                    mode_tag = "[PERSONALIZED]"
                else:
                    subject = render(subject_template, contact)
                    body    = render(body_template, contact)
                    mode_tag = "[GENERIC TEMPLATE]"
                print(f"         {mode_tag}")
                print(f"         Subject : {subject}")
                print(f"         Body preview: {body[:120].strip()}...")
                print()
                sent_this_run.append(contact)  # track for summary display only, do NOT write to log
                continue

            try:
                msg = build_email(cfg, contact, subject_template, body_template)
                smtp_conn.send_message(msg)
                record_sent(log, email_addr, company)
                sent_this_run.append(contact)
                save_log(cfg["log_path"], log)   # Save after each send (safe against crashes)
                
                # Notify tracking server of successful send
                tracker_url = cfg.get("tracker_url", "").rstrip("/")
                if tracker_url:
                    try:
                        import urllib.request as _urllib
                        import json as _json
                        req = _urllib.Request(
                            f"{tracker_url}/api/log_send",
                            data=_json.dumps({"email": email_addr, "company": company}).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            method="POST"
                        )
                        with _urllib.urlopen(req, timeout=5) as resp:
                            pass
                    except Exception as te:
                        print(f"         Tracker API Log Warning: {te}")
                
                print(f"         SENT")
            except Exception as e:
                print(f"         FAILED: {e}")
                record_failed(cfg["log_path"], contact, e)  # Log to failed_log.json

    finally:
        if smtp_conn:
            smtp_conn.quit()

    # Dry run never writes to the log — nothing was actually sent

    remaining = cfg["daily_limit"] - sent_today(log)
    print_summary(sent_this_run, skipped_count, remaining, dry_run)


if __name__ == "__main__":
    main()
