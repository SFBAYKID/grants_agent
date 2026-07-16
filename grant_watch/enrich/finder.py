"""Contact discovery for an awardee entity (district / city / school).

Pipeline (every step verifiable, per CLAUDE.md rule 1):
  1. Firecrawl SEARCH: "<entity> <state> superintendent | technology director" — find
     the entity's own site / staff pages.
  2. Firecrawl SCRAPE the most promising 1-3 pages to markdown.
  3. Claude EXTRACTION: pull {name, title, email, phone} from the page text only.
  4. CODE-LEVEL VERIFICATION (the anti-hallucination gate): every returned contact
     field (name, title, email, phone) must appear in the scraped text under the
     field-specific verifier, or the candidate is rejected. The model cannot smuggle
     invented contact data past this.

Returns a ContactCandidate or None — None means not_found, which is honest and final
until a human or a better source supplies more.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # Firecrawl search response JSON is runtime-shaped.
from urllib.parse import urlparse

import requests
from anthropic import Anthropic

Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop

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
    official_domain: str = ""
    field_evidence: dict[str, bool] | None = None


@dataclass(frozen=True)
class LinkedInPerson:
    """One possible person copied from a LinkedIn search-result listing."""

    name: str
    title: str
    url: str


def verify_on_page(page_text: str, email: str, name: str) -> bool:
    """THE anti-hallucination gate (pure, unit-tested): the email must appear
    VERBATIM in the fetched page, and the first two name tokens must appear too.
    A model claim that fails this is discarded — no exceptions, no overrides."""
    low = page_text.lower()
    if not email or not _EMAIL_RE.fullmatch(email) or email.lower() not in low:
        return False
    return bool(name) and all(tok.lower() in low for tok in name.split()[:2])


def _text_field_on_page(page_text: str, value: str) -> bool:
    """Verify a non-email field through normalized contiguous page text."""
    if not value.strip():
        return False
    normalized_value = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    normalized_page = re.sub(r"[^a-z0-9]+", " ", page_text.lower()).strip()
    return normalized_value in normalized_page


def _phone_on_page(page_text: str, phone: str) -> bool:
    """Verify phone digits without depending on source formatting punctuation."""
    wanted = re.sub(r"\D", "", phone)
    page_digits = re.sub(r"\D", "", page_text)
    return len(wanted) >= 7 and wanted in page_digits


_GENERIC_ENTITY_WORDS = {
    "school", "schools", "district", "unified", "public", "city", "town",
    "county", "of", "the", "department", "board", "education", "isd", "usd",
}
_BLOCKED_HOSTS = {
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "greatschools.org", "niche.com", "wikipedia.org",
}


def _host(url: str) -> str:
    """Return a normalized hostname for official-site binding."""
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _same_site(left: str, right: str) -> bool:
    """Accept exact host or a direct subdomain relationship, not unrelated domains."""
    return bool(left and right and (
        left == right or left.endswith(f".{right}") or right.endswith(f".{left}")
    ))


def _looks_official(entity: str, state: str, result: dict[str, Any]) -> bool:
    """Conservatively bind a search result to the named organization."""
    url = str(result.get("url") or "")
    host = _host(url)
    if not host or any(host == blocked or host.endswith(f".{blocked}")
                       for blocked in _BLOCKED_HOSTS):
        return False
    haystack = " ".join(str(result.get(key) or "")
                        for key in ("title", "description", "url")).lower()
    normalized_entity = re.sub(r"[^a-z0-9]+", " ", entity.lower()).strip()
    normalized_haystack = re.sub(r"[^a-z0-9]+", " ", haystack).strip()
    if normalized_entity and normalized_entity in normalized_haystack:
        return True
    tokens = {
        token for token in normalized_entity.split()
        if token not in _GENERIC_ENTITY_WORDS and len(token) > 2
    }
    if len(tokens) >= 2 and tokens.issubset(set(normalized_haystack.split())):
        return True
    # A one-word distinctive name such as Orange needs both domain and organization
    # phrase evidence; name overlap alone caused a verified CRM false positive.
    if len(tokens) == 1:
        token = next(iter(tokens))
        org_word = any(word in normalized_haystack for word in ("school", "district", "city"))
        state_seen = not state or state.lower() in normalized_haystack
        return token in host and org_word and state_seen
    return False


def _fc_headers() -> dict[str, str]:
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not configured")
    return {"Authorization": f"Bearer {key}"}


def _search(query: str, limit: int = 5) -> list[dict[str, Any]]:
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
    client = Anthropic(timeout=40.0, max_retries=0)
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
    proposed_title = str(data.get("title") or "").strip()
    proposed_phone = str(data.get("phone") or "").strip()
    title = proposed_title if _text_field_on_page(page_text, proposed_title) else ""
    phone = proposed_phone if _phone_on_page(page_text, proposed_phone) else ""
    confidence = ("high" if title and any(t in title.lower() for t in TARGET_TITLES)
                  else "medium")
    return ContactCandidate(
        name=name, title=title, email=email, phone=phone,
        source_url=source_url, confidence=confidence,
        official_domain=_host(source_url),
        field_evidence={
            "name": True, "email": True, "title": bool(title), "phone": bool(phone),
        },
    )


# Title-targeted searches, most decision-relevant first. Multiple angles because a
# district site may hide emails on one page but expose them on the staff directory.
_SEARCH_ANGLES = (
    '{entity} {state} technology director email',
    '{entity} {state} superintendent contact email',
    '{entity} {state} staff directory',
    '{entity} {state} principal contact',
)


def _candidate_rank(candidate: ContactCandidate) -> tuple[int, int, str]:
    """Rank verified contacts by decision relevance, evidence quality, then name."""
    title = candidate.title.lower()
    title_rank = next(
        (index for index, target in enumerate(TARGET_TITLES) if target in title),
        len(TARGET_TITLES),
    )
    confidence_rank = 0 if candidate.confidence == "high" else 1
    return title_rank, confidence_rank, candidate.name.lower()


def find_contacts(entity: str, state: str, max_pages: int = 10,
                  max_contacts: int = 5,
                  on_progress: Progress | None = None) -> list[ContactCandidate]:
    """Read official-site pages and return several distinct verified decision-makers.

    The search deliberately continues after finding a Technology contact so follow-up
    questions such as "anyone else?" can also return Facilities, Operations, Business,
    or Superintendent contacts when the official site publishes them.

    Return/raise contract is the honesty boundary (Constitution rule 1):
      - non-empty list      -> verbatim-verified contacts,
      - empty list          -> real pages were read but none had a verifiable contact,
      - SourceUnreachable   -> we never actually read a page (search/scrape/extract all
                               failed); the caller must record NOTHING, not not_found.
    """
    say = on_progress or _NOOP
    say("Searching for the contact")
    seen_urls: set[str] = set()
    candidates: dict[str, ContactCandidate] = {}
    reached_search = False     # at least one Firecrawl search returned without error
    pages_read = 0             # real (>=200 char) pages we actually scraped
    clean_extractions = 0      # extractions that completed (gave a definite yes/no)
    official_domain = ""
    for angle in _SEARCH_ANGLES:
        query = angle.format(entity=entity, state=state)
        try:
            results = _search(query, limit=4)
        except (requests.RequestException, RuntimeError):
            continue
        reached_search = True
        for r in results:
            url = r.get("url") or ""
            if not url or url in seen_urls or not _looks_official(entity, state, r):
                continue
            candidate_domain = _host(url)
            if official_domain and not _same_site(candidate_domain, official_domain):
                continue
            official_domain = official_domain or candidate_domain
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
            key = cand.email.strip().lower()
            current = candidates.get(key)
            if current is None or _candidate_rank(cand) < _candidate_rank(current):
                candidates[key] = cand
        if len(seen_urls) > max_pages:
            break
    if candidates:
        ranked = sorted(candidates.values(), key=_candidate_rank)[:max_contacts]
        say(f"Found {len(ranked)} contacts")
        return ranked
    # No candidate. Only claim not_found when we actually looked: reached search, read a
    # real page, AND extracted cleanly from it. Otherwise we could not look — say so.
    if not reached_search or pages_read == 0 or clean_extractions == 0:
        raise SourceUnreachable(f"could not read a source for {entity}")
    return []


def find_contact(entity: str, state: str, max_pages: int = 6,
                 on_progress: Progress | None = None) -> ContactCandidate | None:
    """Compatibility helper returning the highest-ranked verified contact, if any."""
    contacts = find_contacts(
        entity, state, max_pages=max_pages, max_contacts=1,
        on_progress=on_progress)
    return contacts[0] if contacts else None


def _linkedin_result_matches(entity: str, result: dict[str, Any]) -> bool:
    """Require the LinkedIn search listing to name the requested organization."""
    haystack = " ".join(str(result.get(key) or "")
                        for key in ("title", "description")).lower()
    normalized_entity = re.sub(r"[^a-z0-9]+", " ", entity.lower()).strip()
    normalized_haystack = re.sub(r"[^a-z0-9]+", " ", haystack).strip()
    if normalized_entity and normalized_entity in normalized_haystack:
        return True
    tokens = {
        token for token in normalized_entity.split()
        if token not in _GENERIC_ENTITY_WORDS and len(token) > 2
    }
    return len(tokens) >= 2 and tokens.issubset(set(normalized_haystack.split()))


def linkedin_person(entity: str, state: str,
                    on_progress: Progress | None = None) -> LinkedInPerson | None:
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
        url = str(r.get("url") or "")
        if "linkedin.com/in/" not in url or not _linkedin_result_matches(entity, r):
            continue
        title = (r.get("title") or "").split("|")[0].strip()  # "Name - Title - Org"
        parts = [p.strip() for p in title.split(" - ")]
        name = parts[0] if parts else ""
        role = parts[1] if len(parts) > 1 else ""
        if name:
            say(f"Found {name} on LinkedIn")
            return LinkedInPerson(name, role, str(url))
    return None
