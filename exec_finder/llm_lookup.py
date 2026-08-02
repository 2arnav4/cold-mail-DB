"""
llm_lookup.py — ask Groq's LLM who the likely founders/CEO/HR contact of a
company are, using only the model's training knowledge (no web access, no
scraping in this step).

This is deliberately the weakest link in the pipeline: the model is asked to
self-report a confidence level and to say "unknown" rather than guess, but
LLMs still confabulate plausible-sounding names for companies they don't
actually have reliable data on. Track the `confidence` field in the output —
low-confidence / unknown results are the expected failure mode for anything
that isn't a well-known company.
"""
import json
import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "llama-3.3-70b-versatile"

_client = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not set in environment/.env")
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


PROMPT_TEMPLATE = """You are helping identify publicly known leadership contacts for a company, \
strictly from what you already know — you have no web access.

Company: {company}

List up to 3 people who plausibly hold Founder, CEO, or Head of HR / \
People roles at this company, based only on training knowledge you're \
confident about. For each person, give an honest confidence score.

Rules:
- If you are not confident a person is currently accurate, still include them \
  but mark confidence "low".
- If you have no knowledge of this company's leadership at all, return an \
  empty list rather than inventing plausible-sounding names.
- Do not fabricate a name just to fill the list.

Respond with ONLY a JSON array, no prose, in this exact shape:
[{{"name": "Full Name", "title": "Founder/CEO/Head of HR", "confidence": "high|medium|low"}}]
"""


def find_people(company: str) -> list[dict]:
    """Returns a list of {name, title, confidence} dicts. May be empty."""
    client = _get_client()
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(company=company)}],
        temperature=0.2,
        max_tokens=400,
    )
    raw = resp.choices[0].message.content.strip()

    # Models sometimes wrap JSON in ```json fences despite instructions.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        people = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  [llm_lookup] could not parse model output for '{company}': {raw[:200]!r}")
        return []

    if not isinstance(people, list):
        return []

    cleaned = []
    for p in people:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        cleaned.append({
            "name": p.get("name", "").strip(),
            "title": p.get("title", "").strip(),
            "confidence": p.get("confidence", "low").strip().lower(),
        })
    return cleaned
