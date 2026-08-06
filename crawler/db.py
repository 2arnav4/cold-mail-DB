"""Shared database access for the crawler.

Every write in here goes through `upsert_company` / `upsert_contact` so there is
exactly one place that decides what a duplicate is and what confidence a new row
is allowed to claim.

The rule the rest of the package depends on: **a contact row may never be
created with an invented address.** A person discovered without a published
email is stored with `email = NULL`. That keeps the name, title and company --
which is what makes a later published-address match possible -- without adding
another `firstname@domain` to a database that already has 6,029 of them.
SQLite treats NULLs as distinct under a UNIQUE constraint, so any number of
unresolved people can coexist.
"""
import os
import re
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "turso-full.db")

# Confidence tiers, kept identical to score_confidence.py. Imported rather than
# duplicated would be better, but that module is a script; this is the one
# constant worth repeating and it is asserted in tests.
CONF_PUBLISHED = 100
CONF_LISTED = 75

TRACKING_PREFIXES = ("www.", "m.", "web.")
BAD_DOMAINS = {
    "ycombinator.com", "workatastartup.com", "linkedin.com", "twitter.com",
    "x.com", "facebook.com", "crunchbase.com", "github.com", "medium.com",
    "notion.site", "google.com", "youtube.com", "instagram.com", "bit.ly",
    "angel.co", "wellfound.com", "gmail.com", "substack.com",
}

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9\-]+)+$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def clean_domain(raw: str) -> str | None:
    """Reduce a URL or bare host to a registrable domain, or None if unusable.

    Rejects the aggregator domains in BAD_DOMAINS: a company whose only listed
    'website' is its LinkedIn page gives us nothing to crawl for an address, and
    storing it means the harvester wastes a request rediscovering that."""
    if not raw:
        return None
    d = raw.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0]
    d = d.split("@")[-1]          # tolerate an email being passed in
    d = d.split(":")[0]           # strip a port
    for p in TRACKING_PREFIXES:
        if d.startswith(p):
            d = d[len(p):]
    if not d or not DOMAIN_RE.match(d):
        return None
    if d in BAD_DOMAINS or any(d.endswith("." + b) for b in BAD_DOMAINS):
        return None
    return d


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def ensure_schema(con: sqlite3.Connection) -> list[str]:
    """Add the columns the crawler needs. Safe to call on every run."""
    added = []

    def add(table, col, decl):
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            added.append(f"{table}.{col}")

    add("companies", "open_roles", "INTEGER DEFAULT 0")
    add("companies", "hiring_checked_at", "DATETIME")
    add("companies", "discovery_source", "VARCHAR(64)")
    add("companies", "careers_url", "VARCHAR(512)")
    add("contacts", "email_provenance", "VARCHAR(16)")
    add("contacts", "confidence_scored_at", "DATETIME")

    con.execute("""CREATE TABLE IF NOT EXISTS crawl_state (
        key         VARCHAR(128) PRIMARY KEY,
        value       TEXT,
        updated_at  DATETIME NOT NULL
    )""")
    con.commit()
    return added


def get_state(con, key: str, default=None):
    row = con.execute("SELECT value FROM crawl_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO crawl_state (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), utc_now()),
    )
    con.commit()


def upsert_company(con: sqlite3.Connection, data: dict) -> int | None:
    """Insert or refresh a company. Returns its id, or None if the domain is bad.

    On conflict, only fields we have a *better* value for are overwritten:
    a later crawl that could not read the location must not blank out one an
    earlier crawl found. `open_roles` is always refreshed, because a stale
    hiring count is worse than none -- it is the signal the sender ranks on."""
    domain = clean_domain(data.get("domain", ""))
    if not domain:
        return None
    name = (data.get("name") or domain).strip()[:255]
    now = utc_now()

    row = con.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
    if row:
        con.execute(
            """UPDATE companies SET
                 name              = COALESCE(NULLIF(?,''), name),
                 industry          = COALESCE(NULLIF(?,''), industry),
                 location          = COALESCE(NULLIF(?,''), location),
                 batch             = COALESCE(NULLIF(?,''), batch),
                 description       = COALESCE(NULLIF(?,''), description),
                 careers_url       = COALESCE(NULLIF(?,''), careers_url),
                 open_roles        = ?,
                 hiring_checked_at = ?,
                 discovery_source  = COALESCE(discovery_source, ?)
               WHERE id = ?""",
            (name, data.get("industry", ""), data.get("location", ""),
             data.get("batch", ""), data.get("description", ""),
             data.get("careers_url", ""), int(data.get("open_roles") or 0), now,
             data.get("source", ""), row["id"]),
        )
        return row["id"]

    cur = con.execute(
        """INSERT INTO companies
             (domain, name, source, industry, location, batch, description,
              funding_stage, created_at, open_roles, hiring_checked_at,
              discovery_source, careers_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (domain, name, data.get("source", "crawler"), data.get("industry", ""),
         data.get("location", ""), data.get("batch", ""),
         data.get("description", ""), data.get("funding_stage", ""), now,
         int(data.get("open_roles") or 0), now, data.get("source", ""),
         data.get("careers_url", "")),
    )
    return cur.lastrowid


def upsert_contact(con: sqlite3.Connection, company_id: int, data: dict) -> str:
    """Insert or improve one contact. Returns 'inserted' | 'updated' | 'skipped'.

    An address is only written when `data['email']` is set *and* the caller
    supplies the provenance that justifies it. Callers that merely know a
    person's name pass email=None; see the module docstring."""
    email = (data.get("email") or "").strip().lower() or None
    name = (data.get("name") or "").strip()[:255] or None
    role = (data.get("role") or "").strip()[:128] or None
    provenance = data.get("provenance") or ("published" if email else None)
    confidence = data.get("confidence")
    if confidence is None:
        confidence = CONF_PUBLISHED if provenance == "published" else None
    now = utc_now()

    if email:
        existing = con.execute(
            "SELECT id, email_confidence, company_id FROM contacts WHERE LOWER(email) = ?",
            (email,)).fetchone()
        if existing:
            # Never downgrade. A published address found twice stays published;
            # a weaker later finding must not overwrite a stronger earlier one.
            if (existing["email_confidence"] or 0) >= (confidence or 0):
                return "skipped"
            con.execute(
                """UPDATE contacts SET email_confidence=?, email_provenance=?,
                       confidence_scored_at=?, name=COALESCE(?,name),
                       role=COALESCE(?,role), source_url=COALESCE(?,source_url),
                       is_invalid=0, invalid_reason=NULL
                   WHERE id=?""",
                (confidence, provenance, now, name, role,
                 data.get("source_url"), existing["id"]),
            )
            return "updated"

    # Same person, already known without an address: fill the address in rather
    # than creating a second row for them.
    if name:
        dup = con.execute(
            "SELECT id, email FROM contacts WHERE company_id=? AND LOWER(name)=?",
            (company_id, name.lower())).fetchone()
        if dup:
            if email and not dup["email"]:
                con.execute(
                    """UPDATE contacts SET email=?, email_confidence=?,
                           email_provenance=?, confidence_scored_at=?,
                           role=COALESCE(?,role), source_url=COALESCE(?,source_url)
                       WHERE id=?""",
                    (email, confidence, provenance, now, role,
                     data.get("source_url"), dup["id"]),
                )
                return "updated"
            return "skipped"

    con.execute(
        """INSERT INTO contacts
             (company_id, name, role, email, email_confidence, email_verified,
              is_invalid, created_at, source, source_url, scraped_pattern,
              email_provenance, confidence_scored_at, linkedin_url, priority)
           VALUES (?,?,?,?,?,0,0,?,?,?,?,?,?,?,?)""",
        (company_id, name, role, email, confidence, now,
         data.get("source", "crawler"), data.get("source_url"),
         "found" if email else None, provenance,
         now if confidence is not None else None,
         data.get("linkedin_url"), int(data.get("priority") or 0)),
    )
    return "inserted"
