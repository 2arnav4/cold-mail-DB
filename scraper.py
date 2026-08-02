#!/usr/bin/env python3
"""
Startup Hiring Scraper with Groq LLM
=====================================
Scrapes startup/company hiring data from multiple sources and populates
turso-full.db (companies + contacts tables) using Groq LLM for extraction.

Sources:
  - YC Company Directory (ycombinator.com/companies)
  - Wellfound / AngelList (public pages)
  - DuckDuckGo / Google custom search queries
  - Naukri (India)
  - Instahyre (India)
  - LinkedIn public job pages

Usage:
  python scraper.py                          # run all sources, limit 30 each
  python scraper.py --source yc --limit 50
  python scraper.py --source google --query "startup hiring HR email India 2026"
  python scraper.py --source naukri --limit 40

Requirements:
  pip install requests beautifulsoup4 groq python-dotenv lxml
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import time
import random
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode, quote_plus

import requests
from bs4 import BeautifulSoup
from groq import Groq
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "turso-full.db")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = "llama-3.3-70b-versatile"
SOURCE_TAG = "groq-scraper"

# ── Candidate profile (used to filter relevant companies) ──────
CANDIDATE = {
    "name": "Arnav Singla",
    "role": "Software Engineering Intern / Full Stack Intern / Backend Intern",
    "skills": [
        "JavaScript", "TypeScript", "React", "Next.js", "Node.js", "Express.js",
        "Go", "PostgreSQL", "MongoDB", "SQLite", "REST APIs", "Redux",
        "Tailwind CSS", "HTML", "CSS", "Git", "Docker",
    ],
    "industries": ["SaaS", "FinTech", "EdTech", "B2B", "Developer Tools", "AI", "Web3"],
    "job_types": ["internship", "full-time"],
    "location": "India (Delhi/Remote)",
    "grad_year": 2028,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# HTTP session
# ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def safe_get(url: str, timeout: int = 15, retries: int = 2, extra_headers: dict = None) -> Optional[requests.Response]:
    headers = {**HEADERS, **(extra_headers or {})}
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, timeout=timeout, headers=headers)
            if resp.status_code == 200:
                return resp
            elif resp.status_code == 429:
                wait = 30 + random.randint(5, 15)
                log.warning(f"Rate limited. Sleeping {wait}s …")
                time.sleep(wait)
            elif resp.status_code in (403, 401):
                log.warning(f"  Blocked ({resp.status_code}): {url[:70]}")
                return None
            else:
                log.warning(f"  HTTP {resp.status_code}: {url[:70]}")
                return None
        except Exception as e:
            log.warning(f"  Request error: {e}")
            if attempt < retries:
                time.sleep(3)
    return None


def polite_sleep(min_s: float = 1.5, max_s: float = 4.0):
    time.sleep(random.uniform(min_s, max_s))


def clean_domain(raw: str) -> str:
    """Strip protocol/www/path from a URL to get bare domain."""
    if not raw:
        return ""
    d = re.sub(r"https?://(www\.)?", "", raw.strip()).strip("/")
    d = d.split("/")[0].split("?")[0].lower()
    return d


# ──────────────────────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def upsert_company(conn: sqlite3.Connection, data: dict) -> Optional[int]:
    domain = clean_domain(data.get("domain", ""))
    if not domain:
        return None
    row = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
    if row:
        log.debug(f"  Company exists: {domain}")
        return row["id"]
    now = datetime.utcnow().isoformat()
    try:
        cur = conn.execute(
            """INSERT INTO companies (domain, name, source, article_url, funding_stage, industry, description, created_at, batch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                domain,
                (data.get("name") or domain)[:255],
                SOURCE_TAG,
                (data.get("article_url") or "")[:1024] or None,
                (data.get("funding_stage") or "")[:32] or None,
                (data.get("industry") or "")[:64] or None,
                (data.get("description") or "")[:2000] or None,
                now,
                (data.get("batch") or "")[:16] or None,
            ),
        )
        conn.commit()
        log.info(f"  ✅ Company: {data.get('name', domain)} ({domain})")
        return cur.lastrowid
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
        return row["id"] if row else None


def upsert_contact(conn: sqlite3.Connection, company_id: int, data: dict) -> bool:
    email = (data.get("email") or "").strip().lower()
    if not email or not re.match(r"^[\w.+\-]+@[\w\-]+\.[a-z]{2,}$", email, re.I):
        return False
    if conn.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone():
        log.debug(f"  Contact exists: {email}")
        return False
    now = datetime.utcnow().isoformat()
    try:
        conn.execute(
            """INSERT INTO contacts
               (company_id, name, role, email, email_confidence, email_verified, is_invalid,
                linkedin_url, created_at, scraped_pattern, notes, source, priority)
               VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                (data.get("name") or "")[:255] or None,
                (data.get("role") or "")[:128] or None,
                email,
                int(data.get("email_confidence") or 50),
                (data.get("linkedin_url") or "")[:512] or None,
                now,
                (data.get("scraped_pattern") or "")[:64] or None,
                data.get("notes") or None,
                SOURCE_TAG,
                int(data.get("priority") or 0),
            ),
        )
        conn.commit()
        log.info(f"  ✅ Contact: {data.get('name', '?')} <{email}>")
        return True
    except sqlite3.IntegrityError:
        return False


# ──────────────────────────────────────────────────────────────
# Groq LLM extraction
# ──────────────────────────────────────────────────────────────

_groq_client: Optional[Groq] = None


def get_groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        if not GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY not set in .env")
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


EXTRACT_PROMPT = """Extract structured hiring/company information from the text below.
Return ONLY a valid JSON object with these fields (null for missing):
{{
  "company_name": "string or null",
  "domain": "bare domain e.g. company.com — no http/www",
  "industry": "e.g. FinTech, SaaS, EdTech, HealthTech or null",
  "funding_stage": "e.g. Seed, Series A, Series B, Pre-Seed or null",
  "description": "1-2 sentence summary or null",
  "is_hiring_engineers": true or false,
  "open_roles": ["list of job titles mentioned, e.g. Software Engineer Intern, Backend Dev"],
  "contacts": [
    {{
      "name": "full name or null",
      "role": "e.g. Head of HR, Talent Acquisition, Recruiter, Founder or null",
      "email": "email address or null",
      "linkedin_url": "linkedin URL or null",
      "email_confidence": 0-100,
      "notes": "useful context or null"
    }}
  ]
}}
Rules:
- Only list HR/recruiting/founder contacts who handle hiring
- If you see an email pattern in the page (e.g. first@domain.com), use it to infer emails for other names
- domain must be bare (no protocol, no path)
- Return ONLY the JSON

Text:
{text}"""

INFER_EMAIL_PROMPT = """Given this person's name and company domain, what is their most likely professional email?

Name: {name}
Domain: {domain}

Common patterns: firstname@domain, firstname.lastname@domain, f.lastname@domain

Return ONLY JSON:
{{
  "email": "most likely email",
  "pattern": "pattern used",
  "confidence": 0-100
}}"""


def extract_with_groq(raw_text: str, source_url: str = "") -> Optional[dict]:
    if not raw_text or len(raw_text.strip()) < 80:
        return None
    text = raw_text[:7000]
    try:
        chat = get_groq().chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        data = json.loads(chat.choices[0].message.content)
        data["article_url"] = source_url
        return data
    except Exception as e:
        log.error(f"  Groq extraction error: {e}")
        return None


def infer_email(name: str, domain: str) -> Optional[dict]:
    if not name or not domain:
        return None
    try:
        chat = get_groq().chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": INFER_EMAIL_PROMPT.format(name=name, domain=domain)}],
            temperature=0.1,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        return json.loads(chat.choices[0].message.content)
    except Exception as e:
        log.debug(f"  Email infer error: {e}")
        return None


def page_to_text(html: str) -> str:
    """Strip scripts/styles and return clean text from HTML."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


# ──────────────────────────────────────────────────────────────
# Relevance filter — only save companies relevant to resume
# ──────────────────────────────────────────────────────────────

RELEVANCE_PROMPT = """You are a job-fit filter.

Candidate profile:
- Seeking: {role}
- Skills: {skills}
- Preferred industries: {industries}
- Location: {location}

Company info:
- Name: {company_name}
- Industry: {industry}
- Description: {description}
- Open roles mentioned: {open_roles}
- Is hiring engineers: {is_hiring_engineers}

Question: Is this company likely to have open roles matching the candidate's profile
(software engineering intern, full stack, backend, frontend, Node.js, React, TypeScript, Go)?

Return ONLY JSON:
{{"relevant": true or false, "reason": "one sentence"}}"""


def is_relevant(entry: dict) -> bool:
    """Use Groq to decide if this company is relevant to the candidate's resume."""
    try:
        prompt = RELEVANCE_PROMPT.format(
            role=CANDIDATE["role"],
            skills=", ".join(CANDIDATE["skills"]),
            industries=", ".join(CANDIDATE["industries"]),
            location=CANDIDATE["location"],
            company_name=entry.get("company_name") or "Unknown",
            industry=entry.get("industry") or "Unknown",
            description=(entry.get("description") or "")[:400],
            open_roles=entry.get("open_roles") or [],
            is_hiring_engineers=entry.get("is_hiring_engineers", True),
        )
        chat = get_groq().chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=80,
            response_format={"type": "json_object"},
        )
        result = json.loads(chat.choices[0].message.content)
        relevant = result.get("relevant", True)
        reason = result.get("reason", "")
        if not relevant:
            log.info(f"  ⏭  Skipping (not relevant): {entry.get('company_name')} — {reason}")
        return relevant
    except Exception as e:
        log.debug(f"  Relevance check error: {e}")
        return True  # default: keep if unsure


# ──────────────────────────────────────────────────────────────
# Source 1: YC Company Directory
# ──────────────────────────────────────────────────────────────

def scrape_internshala(limit: int = 30) -> list[dict]:
    """
    Scrape Internshala — India's largest internship platform.
    No bot protection, no login required, rich startup data.
    """
    log.info("🔍 Scraping Internshala …")
    results = []
    seen = set()

    search_urls = [
        "https://internshala.com/internships/web-development-internship",
        "https://internshala.com/internships/software-development-internship",
        "https://internshala.com/internships/nodejs-internship",
        "https://internshala.com/internships/react-js-internship",
        "https://internshala.com/internships/full-stack-development-internship",
        "https://internshala.com/internships/backend-development-internship",
        "https://internshala.com/internships/python-internship",
        "https://internshala.com/internships/typescript-internship",
    ]

    for url in search_urls:
        if len(results) >= limit:
            break
        log.info(f"  Internshala: {url.split('/')[-1]}")
        resp = safe_get(url, extra_headers={"Referer": "https://internshala.com/"})
        if not resp:
            polite_sleep(2, 4)
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Each internship card has company name, role, and often a link
        cards = (soup.select(".internship_meta") or
                 soup.select(".individual_internship") or
                 soup.select("[id^='internshiplist']"))

        if cards:
            log.info(f"    Found {len(cards)} internship cards")
            for card in cards[:limit]:
                card_text = card.get_text(separator=" ", strip=True)
                if len(card_text) < 30:
                    continue

                # Try to extract company link
                co_link = card.select_one("a.company-name") or card.select_one(".company-name a")
                company_name = ""
                company_url = ""
                if co_link:
                    company_name = co_link.get_text(strip=True)
                    company_url = co_link.get("href", "")

                # Use Groq to extract structured data
                extracted = extract_with_groq(card_text, url)
                if not extracted:
                    if company_name:
                        # Build minimal entry
                        domain = clean_domain(company_url) or f"{re.sub(r'[^a-z0-9]', '', company_name.lower())}.com"
                        extracted = {
                            "company_name": company_name,
                            "domain": domain,
                            "industry": "Tech",
                            "contacts": [],
                        }
                    else:
                        continue

                d = clean_domain(extracted.get("domain", ""))
                if d and d not in seen:
                    seen.add(d)
                    extracted["article_url"] = url
                    results.append(extracted)

                if len(results) >= limit:
                    break

        else:
            # Full page Groq fallback
            text = page_to_text(resp.text)
            extracted = extract_with_groq(text, url)
            if extracted and extracted.get("domain"):
                d = clean_domain(extracted["domain"])
                if d not in seen:
                    seen.add(d)
                    results.append(extracted)

        polite_sleep(2, 4)

    log.info(f"Internshala: collected {len(results)} companies")
    return results


def scrape_hackernews(limit: int = 30) -> list[dict]:
    """
    Scrape HackerNews 'Who is Hiring' monthly threads.
    Completely open, no blocking, real startup hiring posts with emails.
    """
    log.info("🔍 Scraping HackerNews Who's Hiring …")
    results = []
    seen = set()

    # HN Algolia API — fully open, no auth needed
    # Search for "Who is Hiring" threads from 2026
    hn_api = "https://hn.algolia.com/api/v1/search"

    # Find the latest "Ask HN: Who is hiring" thread IDs
    params = {
        "query": "Ask HN: Who is hiring",
        "tags": "story",
        "hitsPerPage": 5,
    }
    try:
        resp = requests.get(hn_api, params=params, timeout=15)
        threads = resp.json().get("hits", [])
        log.info(f"  HN: found {len(threads)} hiring threads")
    except Exception as e:
        log.error(f"  HN thread search error: {e}")
        return results

    for thread in threads[:3]:  # Last 3 months
        thread_id = thread.get("objectID")
        if not thread_id:
            continue
        title = thread.get("title", "")
        log.info(f"  HN thread: {title}")

        # Get comments (job posts) from this thread via Algolia
        comment_params = {
            "tags": f"comment,story_{thread_id}",
            "hitsPerPage": 50,
        }
        try:
            cresp = requests.get(hn_api, params=comment_params, timeout=15)
            comments = cresp.json().get("hits", [])
            log.info(f"    {len(comments)} job posts in thread")
        except Exception as e:
            log.error(f"  HN comments error: {e}")
            continue

        for comment in comments:
            if len(results) >= limit:
                break
            text = comment.get("comment_text") or ""
            if not text or len(text) < 100:
                continue

            # Only process posts mentioning relevant tech
            keywords = ["react", "node", "typescript", "python", "javascript",
                        "full stack", "backend", "frontend", "software engineer",
                        "intern", "remote", "api"]
            text_lower = text.lower()
            if not any(kw in text_lower for kw in keywords):
                continue

            # Strip HTML tags
            clean_text = re.sub(r"<[^>]+>", " ", text)
            clean_text = re.sub(r"\s+", " ", clean_text).strip()

            extracted = extract_with_groq(
                clean_text,
                f"https://news.ycombinator.com/item?id={thread_id}"
            )
            if extracted and extracted.get("domain"):
                d = clean_domain(extracted["domain"])
                if d and d not in seen:
                    seen.add(d)
                    results.append(extracted)

            polite_sleep(0.3, 0.8)

        polite_sleep(2, 3)

    log.info(f"HackerNews: collected {len(results)} companies")
    return results



# ──────────────────────────────────────────────────────────────
# Source 2: DuckDuckGo / Google Search
# ──────────────────────────────────────────────────────────────

# Resume-targeted search queries for Arnav Singla
# Full Stack / Backend / SWE Intern — React, Node.js, TypeScript, Go
GOOGLE_QUERIES = [
    'startup hiring "software engineer intern" OR "full stack intern" India 2026 email',
    'startup "backend engineer" OR "backend developer" intern india "hr@" OR "careers@" 2026',
    'YC startup hiring "react" OR "next.js" OR "node.js" intern email contact 2026',
    'startup hiring "typescript" OR "nodejs" intern india email apply 2026',
    'funded startup india "software intern" OR "swe intern" contact email 2026',
    '"series a" OR "series b" OR "seed" startup india hiring engineer intern email 2026',
    'startup "full stack" OR "fullstack" intern hiring india email recruiter 2026',
    'SaaS OR FinTech OR EdTech startup india hiring intern "apply" email 2026',
    'YC W25 OR W26 OR S25 OR S26 startup hiring engineer intern email',
    'startup delhi OR bangalore OR remote india "software engineer" intern hiring email 2026',
]


def scrape_ddg(query: str, limit: int = 8) -> list[dict]:
    """Search DuckDuckGo and extract company+contact data via Groq from result pages."""
    log.info(f"  🔎 DDG: {query[:70]}…")
    results = []

    try:
        resp = SESSION.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            timeout=15,
        )
        if resp.status_code != 200:
            return results

        soup = BeautifulSoup(resp.text, "lxml")
        links = []
        for a in soup.select(".result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                href = requests.utils.unquote(href.split("uddg=")[-1])
            if href.startswith("http"):
                links.append(href)

        log.info(f"    Found {len(links)} links")

        skip = ["linkedin.com/login", "youtube.com", "facebook.com", "twitter.com",
                "instagram.com", "reddit.com", "wikipedia.org"]

        for href in links[:limit]:
            if any(s in href for s in skip):
                continue
            page = safe_get(href, timeout=12)
            if not page:
                continue
            text = page_to_text(page.text)
            extracted = extract_with_groq(text, href)
            if extracted and extracted.get("domain"):
                results.append(extracted)
            polite_sleep(2, 4)

    except Exception as e:
        log.error(f"  DDG error: {e}")

    return results


def scrape_google_query(query: str, limit: int = 8) -> list[dict]:
    """
    Search Google directly using googlesearch-python and extract data via Groq.
    Falls back to DuckDuckGo if Google blocks or library is missing.
    """
    log.info(f"  🔎 Google: {query[:70]}…")
    results = []
    links = []

    skip = ["linkedin.com/login", "youtube.com", "facebook.com", "twitter.com",
            "instagram.com", "reddit.com", "wikipedia.org", "quora.com",
            "glassdoor.com", "google.com", "amazon.com", "flipkart.com"]

    # ── Try googlesearch-python first ──────────────────────────
    try:
        from googlesearch import search as gsearch
        log.info(f"    Using googlesearch-python…")
        for url in gsearch(query, num_results=limit * 2, sleep_interval=2, lang="en"):
            if url and url.startswith("http") and not any(s in url for s in skip):
                links.append(url)
                if len(links) >= limit:
                    break
        log.info(f"    Google returned {len(links)} links")
    except ImportError:
        log.warning("    googlesearch-python not installed — falling back to DuckDuckGo")
        return scrape_ddg(query, limit=limit)
    except Exception as e:
        log.warning(f"    Google search error ({e}) — falling back to DuckDuckGo")
        return scrape_ddg(query, limit=limit)

    # ── Fetch each result page and extract with Groq ───────────
    for href in links:
        page = safe_get(href, timeout=12)
        if not page:
            continue
        text = page_to_text(page.text)
        extracted = extract_with_groq(text, href)
        if extracted and extracted.get("domain"):
            results.append(extracted)
        polite_sleep(2, 4)

    return results


def scrape_google(limit: int = 30, custom_query: str = None) -> list[dict]:
    """Run all resume-targeted queries across Google + DuckDuckGo and aggregate."""
    log.info("🔍 Scraping via Google + DuckDuckGo …")
    all_results = []
    seen = set()
    queries = [custom_query] if custom_query else GOOGLE_QUERIES

    per_query = max(4, limit // len(queries))

    for i, q in enumerate(queries):
        if len(all_results) >= limit:
            break
        # Alternate between Google and DDG to reduce rate limiting
        if i % 2 == 0:
            entries = scrape_google_query(q, limit=per_query)
        else:
            entries = scrape_ddg(q, limit=per_query)
        for e in entries:
            d = clean_domain(e.get("domain", ""))
            if d and d not in seen:
                seen.add(d)
                all_results.append(e)
        polite_sleep(3, 6)

    log.info(f"Google+DDG: collected {len(all_results)} companies")
    return all_results


# ──────────────────────────────────────────────────────────────
# Source 3: Wellfound (AngelList)
# ──────────────────────────────────────────────────────────────



# ──────────────────────────────────────────────────────────────
# Source 4: Naukri
# ──────────────────────────────────────────────────────────────

def scrape_naukri(limit: int = 20) -> list[dict]:
    """Scrape Naukri for startup HR/recruiter contacts."""
    log.info("🔍 Scraping Naukri …")
    results = []
    seen = set()

    search_terms = [
        "software-engineer-intern-startup-jobs-in-india",
        "full-stack-developer-intern-startup-jobs",
        "backend-developer-intern-startup-jobs",
        "nodejs-developer-intern-startup-jobs",
        "react-developer-intern-startup-jobs-in-delhi",
        "typescript-developer-intern-startup-jobs",
    ]

    for term in search_terms:
        if len(results) >= limit:
            break
        url = f"https://www.naukri.com/{term}"
        resp = safe_get(url, extra_headers={
            "Referer": "https://www.naukri.com/",
            "Accept": "text/html,application/xhtml+xml",
        })
        if not resp:
            polite_sleep(3, 5)
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Try to find job listing JSON in page scripts
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if item.get("@type") != "JobPosting":
                        continue
                    hiring_org = item.get("hiringOrganization") or {}
                    company_name = hiring_org.get("name", "")
                    raw_url = hiring_org.get("sameAs") or hiring_org.get("url") or ""
                    domain = clean_domain(raw_url)
                    if not domain:
                        slug = re.sub(r"[^a-z0-9]", "", company_name.lower())
                        domain = f"{slug}.com"
                    if domain in seen:
                        continue
                    seen.add(domain)

                    entry = {
                        "company_name": company_name,
                        "domain": domain,
                        "industry": item.get("industry"),
                        "funding_stage": None,
                        "description": item.get("description", "")[:400],
                        "article_url": item.get("url") or url,
                        "contacts": [],
                    }
                    results.append(entry)
                    if len(results) >= limit:
                        break
            except (json.JSONDecodeError, AttributeError):
                pass

        # Fallback: Groq from page text
        if not results:
            text = page_to_text(resp.text)
            extracted = extract_with_groq(text, url)
            if extracted and extracted.get("domain"):
                d = clean_domain(extracted["domain"])
                if d not in seen:
                    seen.add(d)
                    results.append(extracted)

        polite_sleep(3, 5)

    # For each company found, try to infer HR email
    for entry in results:
        if not entry.get("contacts") and entry.get("company_name") and entry.get("domain"):
            for role_hint in ["HR Manager", "Talent Acquisition", "Recruiter"]:
                generic_names = ["HR Team", "Talent Team"]
                for name in generic_names:
                    inferred = infer_email(name, entry["domain"])
                    if inferred and inferred.get("email"):
                        entry["contacts"].append({
                            "name": None,
                            "role": role_hint,
                            "email": inferred["email"],
                            "email_confidence": inferred.get("confidence", 30),
                            "scraped_pattern": inferred.get("pattern"),
                            "notes": f"Inferred HR contact at {entry['company_name']}",
                        })
                        break

    log.info(f"Naukri: collected {len(results)} companies")
    return results


# ──────────────────────────────────────────────────────────────
# Source 5: LinkedIn public job search (no auth)
# ──────────────────────────────────────────────────────────────

def scrape_linkedin(limit: int = 20) -> list[dict]:
    """Scrape LinkedIn's public job listings (no login needed)."""
    log.info("🔍 Scraping LinkedIn public jobs …")
    results = []
    seen = set()

    searches = [
        "software+engineer+intern+startup+india",
        "full+stack+intern+startup+india",
        "backend+developer+intern+startup",
        "nodejs+react+intern+startup+india",
        "typescript+developer+intern+startup",
    ]

    for kw in searches:
        if len(results) >= limit:
            break
        url = f"https://www.linkedin.com/jobs/search/?keywords={kw}&f_TPR=r604800&position=1&pageNum=0"
        resp = safe_get(url)
        if not resp:
            polite_sleep(4, 7)
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.select(".jobs-search__results-list li") or soup.select(".base-card")

        if not cards:
            # Full-page Groq fallback
            text = page_to_text(resp.text)
            extracted = extract_with_groq(text, url)
            if extracted and extracted.get("domain"):
                d = clean_domain(extracted["domain"])
                if d not in seen:
                    seen.add(d)
                    results.append(extracted)
        else:
            for card in cards[:limit]:
                card_text = card.get_text(separator=" ", strip=True)
                job_link = card.select_one("a.base-card__full-link")
                job_url = job_link["href"] if job_link else url

                # Fetch the individual job page for richer data
                if job_url != url:
                    jp = safe_get(job_url, timeout=10)
                    if jp:
                        card_text = page_to_text(jp.text)
                        polite_sleep(1, 3)

                extracted = extract_with_groq(card_text, job_url)
                if extracted and extracted.get("domain"):
                    d = clean_domain(extracted["domain"])
                    if d not in seen:
                        seen.add(d)
                        results.append(extracted)
                if len(results) >= limit:
                    break

        polite_sleep(4, 7)

    log.info(f"LinkedIn: collected {len(results)} companies")
    return results


# ──────────────────────────────────────────────────────────────
# Source 6: Instahyre
# ──────────────────────────────────────────────────────────────

def scrape_instahyre(limit: int = 20) -> list[dict]:
    """Scrape Instahyre for India startup hiring contacts."""
    log.info("🔍 Scraping Instahyre …")
    results = []
    seen = set()

    urls = [
        "https://www.instahyre.com/search-jobs/?q=software+engineer&type=startup",
        "https://www.instahyre.com/search-jobs/?q=intern&type=startup",
        "https://www.instahyre.com/employer-list/",
    ]

    for base_url in urls:
        if len(results) >= limit:
            break
        resp = safe_get(base_url)
        if not resp:
            polite_sleep(3, 5)
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Try LD+JSON
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if item.get("@type") != "JobPosting":
                        continue
                    org = item.get("hiringOrganization") or {}
                    company_name = org.get("name", "")
                    raw_url = org.get("sameAs") or org.get("url") or ""
                    domain = clean_domain(raw_url)
                    if not domain:
                        domain = f"{re.sub(r'[^a-z0-9]', '', company_name.lower())}.com"
                    if domain in seen:
                        continue
                    seen.add(domain)
                    results.append({
                        "company_name": company_name,
                        "domain": domain,
                        "industry": None,
                        "funding_stage": None,
                        "description": item.get("description", "")[:400],
                        "article_url": base_url,
                        "contacts": [],
                    })
            except (json.JSONDecodeError, AttributeError):
                pass

        # Groq page-level fallback
        if not results:
            text = page_to_text(resp.text)
            extracted = extract_with_groq(text, base_url)
            if extracted and extracted.get("domain"):
                d = clean_domain(extracted["domain"])
                if d not in seen:
                    seen.add(d)
                    results.append(extracted)

        polite_sleep(2, 4)

    log.info(f"Instahyre: collected {len(results)} companies")
    return results


# ──────────────────────────────────────────────────────────────
# Pipeline: save to DB
# ──────────────────────────────────────────────────────────────

def process_and_save(conn: sqlite3.Connection, entries: list[dict]) -> tuple[int, int]:
    companies_added = 0
    contacts_added = 0

    for entry in entries:
        if not entry:
            continue
        domain = clean_domain(entry.get("domain", ""))
        if not domain:
            continue

        # ── Relevance gate: skip companies that don't match the resume ──
        if not is_relevant(entry):
            continue

        company_id = upsert_company(conn, {
            "domain": domain,
            "name": entry.get("company_name") or domain,
            "article_url": entry.get("article_url"),
            "funding_stage": entry.get("funding_stage"),
            "industry": entry.get("industry"),
            "description": entry.get("description"),
            "batch": entry.get("batch"),
        })

        if company_id is None:
            continue
        companies_added += 1

        for contact in (entry.get("contacts") or []):
            if not contact:
                continue
            # Infer email if missing
            if not contact.get("email") and contact.get("name"):
                inf = infer_email(contact["name"], domain)
                if inf:
                    contact["email"] = inf.get("email")
                    contact["email_confidence"] = inf.get("confidence", 30)
                    contact["scraped_pattern"] = inf.get("pattern")

            if upsert_contact(conn, company_id, contact):
                contacts_added += 1

    return companies_added, contacts_added


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

SOURCES = {
    "internshala":  scrape_internshala,   # India's #1 intern platform — no blocking
    "hackernews":   scrape_hackernews,    # HN Who's Hiring — fully open
    "linkedin":     scrape_linkedin,
    "naukri":       scrape_naukri,
    "instahyre":    scrape_instahyre,
    "google":       scrape_google,
}


def count_scraped(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE source = ?", (SOURCE_TAG,)
    ).fetchone()
    return row[0] if row else 0


def main():
    parser = argparse.ArgumentParser(
        description="Scrape startup hiring data → turso-full.db using Groq LLM"
    )
    parser.add_argument("--limit", type=int, default=30,
        help="Max companies to fetch per source per pass (default: 30)")
    parser.add_argument("--total-limit", type=int, default=None,
        help="Keep running until this many companies are in DB (e.g. 1000)")
    parser.add_argument("--source",
        choices=list(SOURCES.keys()) + ["all"],
        default="all",
        help="Which source to scrape (default: all)")
    parser.add_argument("--query", type=str, default=None,
        help="Custom search query (used with --source google)")
    parser.add_argument("--db", type=str, default=DB_PATH,
        help=f"Path to SQLite DB (default: {DB_PATH})")
    args = parser.parse_args()

    db_path = args.db
    total_limit = args.total_limit

    if not GROQ_API_KEY:
        log.error(
            "❌ GROQ_API_KEY not set!\n"
            "   Add to .env:  GROQ_API_KEY=gsk_xxxx\n"
            "   Free key at:  https://console.groq.com"
        )
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")

    log.info(f"🚀 Starting | source={args.source} | limit/pass={args.limit} | total-limit={total_limit or '∞'}")
    log.info(f"   DB:    {db_path}")
    log.info(f"   Model: {GROQ_MODEL}")

    total_co = total_ct = 0
    pass_num = 0

    try:
        while True:
            pass_num += 1
            in_db = count_scraped(conn)

            if total_limit and in_db >= total_limit:
                log.info(f"\n🎯 Target reached! {in_db}/{total_limit} companies in DB.")
                break

            remaining = (total_limit - in_db) if total_limit else None
            log.info(f"\n{'═'*55}")
            log.info(f"  PASS {pass_num}  |  In DB: {in_db}  |  Remaining: {remaining if remaining is not None else '∞'}")
            log.info(f"{'═'*55}")

            # Scale per-source fetch size based on what's left
            per_source = args.limit
            if remaining is not None:
                n_sources = len(SOURCES) + 1
                per_source = max(5, min(args.limit, remaining // n_sources + 1))

            pass_co = pass_ct = 0

            if args.source == "google":
                entries = scrape_google(limit=per_source, custom_query=args.query)
                c, ct = process_and_save(conn, entries)
                pass_co += c; pass_ct += ct

            elif args.source == "all":
                all_sources = list(SOURCES.items()) + [("google", lambda lim: scrape_google(limit=lim))]
                for name, fn in all_sources:
                    in_db = count_scraped(conn)
                    if total_limit and in_db >= total_limit:
                        break
                    log.info(f"\n  ── {name.upper()} ──")
                    try:
                        entries = fn(per_source)
                        c, ct = process_and_save(conn, entries)
                        pass_co += c; pass_ct += ct
                        log.info(f"  → +{c} companies, +{ct} contacts | DB now: {count_scraped(conn)}")
                    except Exception as e:
                        log.error(f"  {name} failed: {e}")
                    polite_sleep(3, 6)

            else:
                entries = SOURCES[args.source](per_source)
                c, ct = process_and_save(conn, entries)
                pass_co += c; pass_ct += ct

            total_co += pass_co
            total_ct += pass_ct

            # Single-run mode (no --total-limit)
            if not total_limit:
                break

            if pass_co == 0:
                log.info("  ⏸  Nothing new this pass — waiting 60s before retry…")
                time.sleep(60)
            else:
                polite_sleep(5, 10)

    finally:
        final = count_scraped(conn)
        conn.close()

    log.info(f"\n{'='*55}")
    log.info(f"✅ DONE — Session: +{total_co} companies, +{total_ct} contacts")
    log.info(f"   Total groq-scraper entries in DB: {final}")
    log.info(f"{'='*55}")


if __name__ == "__main__":
    main()
