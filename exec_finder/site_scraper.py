"""
site_scraper.py — scrape a company's OWN domain (About/Team/Leadership/Press/
Contact pages only) for names and emails the company has already chosen to
publish. This does not crawl arbitrary third-party sites or search engines,
and it respects robots.txt.

Scope is intentionally narrow: this finds published, self-disclosed contact
info. It will come up empty for the (common) case of a company that doesn't
list a team page or publishes generic-only addresses (info@, hello@) — that
gap is expected and is one of the real limitations of this approach.
"""
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; exec-finder-demo/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}

CANDIDATE_PATHS = [
    "/", "/about", "/about-us", "/team", "/our-team", "/leadership",
    "/company", "/press", "/newsroom", "/contact", "/contact-us",
]

REQUEST_TIMEOUT = 10
DELAY_BETWEEN_REQUESTS = 1.5

EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
GENERIC_LOCALPARTS = {
    "info", "hello", "contact", "support", "sales", "press", "media",
    "careers", "jobs", "hr", "admin", "office", "team", "help",
}


def _robots_allows(base_url: str, path: str) -> bool:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception:
        # No readable robots.txt -> default to allowed for a normal page fetch.
        return True
    return rp.can_fetch(HEADERS["User-Agent"], urljoin(base_url, path))


def _extract_emails_with_context(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")

    found = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        if tag["href"].lower().startswith("mailto:"):
            email = tag["href"].split(":", 1)[1].split("?")[0].strip()
            if email and email.lower() not in seen:
                seen.add(email.lower())
                name = tag.get_text(strip=True) or _nearby_text(tag)
                found.append({"email": email, "name_hint": name})

    text = soup.get_text(" ", strip=True)
    for match in EMAIL_RE.finditer(text):
        email = match.group(0)
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        start = max(0, match.start() - 60)
        found.append({"email": email, "name_hint": text[start:match.start()].strip()[-60:]})

    return found


def _nearby_text(tag) -> str:
    parent = tag.find_parent()
    if parent:
        return parent.get_text(" ", strip=True)[:60]
    return ""


def _is_generic(email: str) -> bool:
    local = email.split("@")[0].lower()
    return local in GENERIC_LOCALPARTS


def scrape_company_site(domain: str) -> list[dict]:
    """Crawl a handful of well-known pages on `domain` for published emails.

    Returns a list of {email, name_hint, source_url, is_generic} dicts.
    Skips any path robots.txt disallows and rate-limits between requests.
    """
    if not domain.startswith("http"):
        base_url = f"https://{domain}"
    else:
        base_url = domain

    results = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for path in CANDIDATE_PATHS:
        if not _robots_allows(base_url, path):
            print(f"  [site_scraper] robots.txt disallows {path}, skipping")
            continue

        url = urljoin(base_url, path)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"  [site_scraper] fetch failed for {url}: {e}")
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        if resp.status_code != 200:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        for item in _extract_emails_with_context(resp.text):
            item["source_url"] = url
            item["is_generic"] = _is_generic(item["email"])
            results.append(item)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # De-dupe across pages, keep first occurrence.
    deduped = {}
    for item in results:
        deduped.setdefault(item["email"].lower(), item)
    return list(deduped.values())
