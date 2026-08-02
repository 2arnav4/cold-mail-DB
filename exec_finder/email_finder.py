"""
email_finder.py — given a person's name and a company domain, generate the
common corporate email-pattern candidates and verify each one against real
mail servers (MX lookup + SMTP RCPT probe), reusing the verification
waterfall already built in ../email_verify.py.

This is pattern-guessing, not scraping: it never looks up the person's real
email anywhere, it only checks whether a *guessed* address is deliverable.
Accuracy is bounded by how well the company's real convention matches one of
the common patterns below — many companies use conventions this list doesn't
cover (e.g. first name only, initials, or a scheme with numbers), which is
one of the real failure points of this whole approach.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from email_verify import verify_email  # noqa: E402


def _name_parts(full_name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
    if not parts:
        return "", ""
    first = re.sub(r"[^a-zA-Z]", "", parts[0]).lower()
    last = re.sub(r"[^a-zA-Z]", "", parts[-1]).lower() if len(parts) > 1 else ""
    return first, last


def generate_candidates(full_name: str, domain: str) -> list[str]:
    first, last = _name_parts(full_name)
    if not first:
        return []

    domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    candidates = [f"{first}@{domain}"]

    if last:
        candidates += [
            f"{first}.{last}@{domain}",
            f"{first}{last}@{domain}",
            f"{first[0]}{last}@{domain}",
            f"{first}.{last[0]}@{domain}",
            f"{first}_{last}@{domain}",
            f"{last}.{first}@{domain}",
            f"{last}{first}@{domain}",
        ]

    # De-dupe, preserve order.
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def find_email(full_name: str, domain: str) -> dict:
    """Tries each generated pattern until one verifies as deliverable.

    Returns {"email": str|None, "checked_by": str|None, "candidates_tried": int}.
    """
    candidates = generate_candidates(full_name, domain)
    for i, candidate in enumerate(candidates, start=1):
        is_valid, checked_by = verify_email(candidate)
        if is_valid:
            return {"email": candidate, "checked_by": checked_by, "candidates_tried": i}

    return {"email": None, "checked_by": None, "candidates_tried": len(candidates)}
