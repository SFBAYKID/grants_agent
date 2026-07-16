"""Official-site organization facts for richer, evidence-backed CRM records.

The model may locate candidate strings, but code verifies every value against fetched
official-site text. Missing or unverified values are omitted; nothing is guessed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any  # Firecrawl and model JSON are runtime-shaped.
from urllib.parse import urlparse

import requests
from anthropic import Anthropic

FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v1/scrape"
_US_POSTAL_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY".split()
)


@dataclass(frozen=True)
class OrganizationProfile:
    """Individually verified official-website organization fields."""

    website: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""
    main_phone: str = ""
    source_url: str = ""
    linkedin_url: str = ""


def _host(value: str) -> str:
    """Normalize one URL/domain to a hostname."""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower().removeprefix("www.")


def _official_url(domain: str) -> str:
    """Return an HTTPS root only for a syntactically plausible official domain."""
    host = _host(domain)
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("verified official domain is required")
    return f"https://{host}/"


def _scrape(url: str) -> str:
    """Fetch one official page through Firecrawl and return its markdown."""
    response = requests.post(
        FIRECRAWL_SCRAPE,
        headers={
            "Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={"url": url, "formats": ["markdown"], "onlyMainContent": False},
        timeout=45,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return str((body.get("data") or {}).get("markdown") or "")


def _normalized(value: str) -> str:
    """Normalize source/value text for conservative contiguous comparisons."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _verified_text(page: str, value: object) -> str:
    """Return a candidate only when it appears contiguously in official page text."""
    candidate = str(value or "").strip()
    return (
        candidate if candidate and _normalized(candidate) in _normalized(page) else ""
    )


def _verified_phone(page: str, value: object) -> str:
    """Verify a phone by its digits despite source punctuation."""
    candidate = str(value or "").strip()
    digits = re.sub(r"\D", "", candidate)
    page_digits = re.sub(r"\D", "", page)
    return candidate if len(digits) >= 10 and digits in page_digits else ""


def extract_profile(
    page: str, official_domain: str, source_url: str, candidate: dict[str, object]
) -> OrganizationProfile:
    """Build a profile only from candidates independently verified on the page."""
    website = _official_url(official_domain)
    street = _verified_text(page, candidate.get("street"))
    city = _verified_text(page, candidate.get("city"))
    state = _verified_text(page, candidate.get("state"))
    postal_code = _verified_text(page, candidate.get("postal_code"))
    country = _verified_text(page, candidate.get("country"))
    # A complete official-page address with a U.S. postal abbreviation is sufficient
    # evidence for the normalized Salesforce country even when the footer omits it.
    if (
        not country
        and street
        and city
        and postal_code
        and state.upper() in _US_POSTAL_CODES
    ):
        country = "United States"
    linkedin = str(candidate.get("linkedin_url") or "").strip()
    if _host(linkedin) != "linkedin.com" and not _host(linkedin).endswith(
        ".linkedin.com"
    ):
        linkedin = ""
    if linkedin and linkedin not in page:
        linkedin = ""
    return OrganizationProfile(
        website=website,
        street=street,
        city=city,
        state=state,
        postal_code=postal_code,
        country=country,
        main_phone=_verified_phone(page, candidate.get("main_phone")),
        source_url=source_url,
        linkedin_url=linkedin,
    )


def fetch_profile(
    entity: str, official_domain: str, contact_source_url: str
) -> OrganizationProfile:
    """Scrape official pages, extract candidates, and code-verify every field."""
    root = _official_url(official_domain)
    urls = list(dict.fromkeys([contact_source_url, root]))
    pages = [
        text
        for url in urls
        if _host(url) == _host(root)
        for text in [_scrape(url)]
        if text
    ]
    if not pages:
        raise RuntimeError("official organization pages were unavailable")
    page = "\n".join(pages)
    prompt = (
        "Return JSON only with street, city, state, postal_code, country, main_phone, "
        "linkedin_url. Copy exact strings from the supplied official page. Use empty "
        f"strings when absent. Organization: {entity}\nOFFICIAL PAGE:\n{page[:60000]}"
    )
    response = Anthropic().messages.create(
        model=os.environ.get("GRANT_MODEL", "claude-sonnet-5"),
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(
        str(block.text)
        for block in response.content
        if getattr(block, "type", "") == "text"
    )
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match is None:
        raise RuntimeError("organization profile extraction returned no JSON")
    candidate: dict[str, object] = json.loads(match.group(0))
    return extract_profile(page, official_domain, urls[0], candidate)
