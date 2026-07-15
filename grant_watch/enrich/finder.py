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
from collections.abc import Callable
from dataclasses import dataclass

import requests
from anthropic import Anthropic

Progress = Callable[[str], None]
_NOOP: Progress = lambda _msg: None

FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v1/search"
FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v1/scrape"
MODEL = "claude-sonnet-5"


class SourceUnreachable(RuntimeError):
    """Contact discovery never actually READ a page — search/scrape/extract all failed
    for infrastructure reasons. The honest outcome is 'we could not look,' which must
    NEVER be recorded as not_found (Constitution rule 1). Callers treat this as a
    retryable non-result and persist nothing."""

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


# Title-targeted searches, most decision-relevant first. Multiple angles because a
# district site may hide emails on one page but expose them on the staff directory.
_SEARCH_ANGLES = (
    '{entity} {state} technology director email',
    '{entity} {state} superintendent contact email',
    '{entity} {state} staff directory',
    '{entity} {state} principal contact',
)


def find_contact(entity: str, state: str, max_pages: int = 6,
                 on_progress: Progress | None = None) -> ContactCandidate | None:
    """Full pipeline for one entity. Runs several title-targeted searches, scrapes the
    most promising pages, and returns the first VERIFIED contact (prefers higher-value
    titles). on_progress emits short (<=6 word) status phrases for Grant's live spinner.

    Return/raise contract is the honesty boundary (Constitution rule 1):
      - a ContactCandidate  -> a verbatim-verified contact,
      - None                -> we genuinely READ real pages and none had a verifiable
                               contact (a truthful not_found),
      - SourceUnreachable   -> we never actually read a page (search/scrape/extract all
                               failed); the caller must record NOTHING, not not_found.
    """
    say = on_progress or _NOOP
    say("Searching for the contact")
    seen_urls: set[str] = set()
    candidates: list[ContactCandidate] = []
    reached_search = False     # at least one Firecrawl search returned without error
    pages_read = 0             # real (>=200 char) pages we actually scraped
    clean_extractions = 0      # extractions that completed (gave a definite yes/no)
    for angle in _SEARCH_ANGLES:
        query = angle.format(entity=entity, state=state)
        try:
            results = _search(query, limit=4)
        except (requests.RequestException, RuntimeError):
            continue
        reached_search = True
        for r in results:
            url = r.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if len(seen_urls) > max_pages:
                break
            say("Reading their website")
            text = _scrape(url)
            if len(text) < 200:  # empty/blocked page — no evidence
                continue
            pages_read += 1
            try:
                cand = _extract(text, entity, url)
            except Exception:  # noqa: BLE001 — one page's API hiccup is inconclusive, not not_found
                continue
            clean_extractions += 1
            if cand is None:
                continue
            if cand.confidence == "high":  # a target-title match — take it immediately
                say(f"Found {cand.name}")
                return cand
            candidates.append(cand)  # medium: hold in case nothing better turns up
        if len(seen_urls) > max_pages:
            break
    if candidates:
        say(f"Found {candidates[0].name}")
        return candidates[0]
    # No candidate. Only claim not_found when we actually looked: reached search, read a
    # real page, AND extracted cleanly from it. Otherwise we could not look — say so.
    if not reached_search or pages_read == 0 or clean_extractions == 0:
        raise SourceUnreachable(f"could not read a source for {entity}")
    return None


def linkedin_person(entity: str, state: str,
                    on_progress: Progress | None = None) -> dict | None:
    """Find the likely decision-maker's LinkedIn profile (name, title, url). No email
    — LinkedIn is login-walled — so this returns a PERSON + profile link to reach out
    through or verify, never a fabricated address. Parsed from the search result, which
    for LinkedIn reads like 'Name - Title - Org | LinkedIn'."""
    say = on_progress or _NOOP
    say("Checking LinkedIn")
    query = (f"site:linkedin.com/in {entity} {state} "
             f"technology director OR superintendent OR principal")
    try:
        results = _search(query, limit=5)
    except (requests.RequestException, RuntimeError):
        return None
    for r in results:
        url = r.get("url") or ""
        if "linkedin.com/in/" not in url:
            continue
        title = (r.get("title") or "").split("|")[0].strip()  # "Name - Title - Org"
        parts = [p.strip() for p in title.split(" - ")]
        name = parts[0] if parts else ""
        role = parts[1] if len(parts) > 1 else ""
        if name:
            say(f"Found {name} on LinkedIn")
            return {"name": name, "title": role, "url": url}
    return None
