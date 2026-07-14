"""Contact discovery for an awardee entity (district / city / school).

Pipeline (every step verifiable, per CLAUDE.md rule 1):
  1. Firecrawl SEARCH: "<entity> <state> superintendent | technology director" — find
     the entity's own site / staff pages.
  2. Firecrawl SCRAPE the most promising 1-3 pages to markdown.
  3. Claude EXTRACTION: pull {name, title, email, phone} from the page text only.
  4. CODE-LEVEL VERIFICATION (the anti-hallucination gate): the email must appear
     VERBATIM in the scraped text, and the name tokens must appear too, or the
     candidate is rejected. The model cannot smuggle an invented address past this.

Returns a ContactCandidate or None — None means not_found, which is honest and final
until a human or a better source supplies more.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import requests
from anthropic import Anthropic

FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v1/search"
FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v1/scrape"
MODEL = "claude-sonnet-5"

# Titles that own security-funding decisions, in rough priority order (CLAUDE.md).
TARGET_TITLES = ("technology director", "director of technology", "it director",
                 "superintendent", "director of facilities", "facilities director",
                 "business manager", "operations director", "city manager")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class ContactCandidate:
    """A verified-on-page contact. source_url is the page the email was found on."""

    name: str
    title: str
    email: str
    phone: str
    source_url: str
    confidence: str  # high | medium | low


def verify_on_page(page_text: str, email: str, name: str) -> bool:
    """THE anti-hallucination gate (pure, unit-tested): the email must appear
    VERBATIM in the fetched page, and the first two name tokens must appear too.
    A model claim that fails this is discarded — no exceptions, no overrides."""
    low = page_text.lower()
    if not email or not _EMAIL_RE.fullmatch(email) or email.lower() not in low:
        return False
    return bool(name) and all(tok.lower() in low for tok in name.split()[:2])


def _fc_headers() -> dict[str, str]:
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not configured")
    return {"Authorization": f"Bearer {key}"}


def _search(query: str, limit: int = 5) -> list[dict]:
    resp = requests.post(FIRECRAWL_SEARCH, headers=_fc_headers(),
                         json={"query": query, "limit": limit}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _scrape(url: str) -> str:
    """One page -> markdown text ('' on failure — a failed scrape is not evidence)."""
    try:
        resp = requests.post(FIRECRAWL_SCRAPE, headers=_fc_headers(),
                             json={"url": url, "formats": ["markdown"]}, timeout=60)
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("markdown") or ""
    except requests.RequestException:
        return ""


def _extract(page_text: str, entity: str, source_url: str) -> ContactCandidate | None:
    """Claude reads ONE page; code verifies every claim against the same page."""
    client = Anthropic()
    prompt = (
        f"Below is a page from the website of (or about) \"{entity}\". Find the best "
        f"contact for technology / security-funding decisions. Priority titles: "
        f"{', '.join(TARGET_TITLES)}.\n\n"
        f"Rules: use ONLY this page's text. The email must be copied EXACTLY as it "
        f"appears. If no suitable person+email is on this page, return null.\n\n"
        f"Respond with ONLY JSON: {{\"name\": \"...\", \"title\": \"...\", "
        f"\"email\": \"...\", \"phone\": \"...\"}} or null.\n\n"
        f"PAGE ({source_url}):\n{page_text[:24000]}"
    )
    msg = client.messages.create(model=MODEL, max_tokens=300,
                                 messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    if raw.lower().startswith("null"):
        return None
    try:
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None

    email = str(data.get("email") or "").strip()
    name = str(data.get("name") or "").strip()
    if not verify_on_page(page_text, email, name):
        return None  # THE GATE said no — model claim not backed by the page
    title = str(data.get("title") or "").strip()
    confidence = "high" if any(t in title.lower() for t in TARGET_TITLES) else "medium"
    return ContactCandidate(name=name, title=title, email=email,
                            phone=str(data.get("phone") or "").strip(),
                            source_url=source_url, confidence=confidence)


def find_contact(entity: str, state: str) -> ContactCandidate | None:
    """Full pipeline for one entity. Tries up to 3 candidate pages; first verified
    contact wins. None = not_found (recorded honestly by the caller)."""
    query = f"{entity} {state} staff directory superintendent technology director email"
    try:
        results = _search(query)
    except (requests.RequestException, RuntimeError):
        return None
    for r in results[:3]:
        url = r.get("url") or ""
        if not url:
            continue
        text = _scrape(url)
        if len(text) < 200:  # empty/blocked page — no evidence to extract from
            continue
        candidate = _extract(text, entity, url)
        if candidate is not None:
            return candidate
    return None
