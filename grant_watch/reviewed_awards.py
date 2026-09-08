"""Shared boundaries for reviewed award documents that are searchable, never pushed.

These sources refresh reviewed cohorts; they do not establish exhaustive statewide
coverage or security procurement eligibility. Parsers must validate a complete cohort
before returning any items. The dedicated namespace is also a proactive exclusion.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup, Tag

from .models import DatePrecision, FundingEventType, RawItem, VerificationStatus

QUIET_SOURCE_PREFIX = "reviewed-school-award:"
CONDITIONAL_SOURCE = QUIET_SOURCE_PREFIX + "pa-pccd-conditional"
PROACTIVE_SOURCE_PREDICATE = "l.source NOT LIKE 'reviewed-school-award:%'"
MAX_BYTES = 12 * 1024 * 1024


def is_reviewed_award(source: object) -> bool:
    """Identify search-only evidence independently of grade, age, or backfill flags."""
    return str(source or "").startswith(QUIET_SOURCE_PREFIX)


def fetch_document(url: str) -> bytes:
    """Fetch a code-reviewed HTTPS URL with bounded bytes and no redirected request."""
    # Lazy import avoids a registry cycle when a consumer imports this policy first.
    from .sources.base import REQUEST_TIMEOUT_S, USER_AGENT

    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("reviewed source must use HTTPS without credentials")
    time.sleep(1)
    with requests.get(
        url,
        timeout=REQUEST_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=False,
        stream=True,
    ) as response:
        if 300 <= response.status_code < 400:
            raise ValueError(
                "reviewed source redirected; review replacement before use"
            )
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("reviewed document exceeds byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def page_text(payload: bytes) -> str:
    """Read visible page content without scripts, styles, or fabricated selectors."""
    soup = BeautifulSoup(payload, "html.parser")
    for node in soup(["script", "style", "nav", "footer"]):
        node.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def section(payload: bytes, selector: str) -> Tag:
    """Require one reviewed article container, excluding unrelated page furniture."""
    matches = BeautifulSoup(payload, "html.parser").select(selector)
    if len(matches) != 1:
        raise ValueError("reviewed article container missing or ambiguous")
    return matches[0]


def require_text(text: str, *phrases: str) -> None:
    """Fail the whole source if reviewed award assertions disappear or drift."""
    for phrase in phrases:
        if phrase not in text:
            raise ValueError(f"reviewed award evidence missing: {phrase[:90]}")


def award_item(
    *,
    source: str,
    entity: str,
    state: str,
    program: str,
    awarded_on: str,
    amount: float | None,
    url: str,
    evidence: str,
    identity: str = "",
    entity_type: str = "",
    start: str = "",
    end: str = "",
    raw: dict[str, object] | None = None,
) -> RawItem:
    """Create one source-bound fact; identity excludes amounts, page order, and fetch date."""
    date.fromisoformat(awarded_on)
    stable = "|".join((program, awarded_on, state, entity, identity)).casefold()
    stable = re.sub(r"\s+", " ", stable).strip()
    return RawItem(
        source=QUIET_SOURCE_PREFIX + source,
        item_id=hashlib.sha256(stable.encode()).hexdigest(),
        entity=entity,
        state=state,
        program=program,
        title=f"{program}: {evidence}"[:500],
        amount=amount,
        start=start,
        end=end,
        url=url,
        raw=raw or {},
        event_type=FundingEventType.AWARD_ANNOUNCED,
        event_date=awarded_on,
        date_precision=DatePrecision.DAY,
        funded_scope=evidence,
        source_locator=identity or entity,
        evidence_excerpt=evidence[:1000],
        verification_status=VerificationStatus.VERIFIED,
        backfill=True,
        entity_type=entity_type,
    )


def validate_keys(items: list[RawItem]) -> list[RawItem]:
    """Reject empty cohorts or colliding source identities rather than losing rows."""
    keys = {(item.source, item.item_id) for item in items}
    if not items or len(keys) != len(items):
        raise ValueError("reviewed award cohort is empty or has duplicate identities")
    return items
