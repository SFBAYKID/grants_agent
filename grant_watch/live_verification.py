"""Opt-in, read-only verification of one real award and its official contact record.

This is intentionally not part of pytest's default network-free suite. It proves that
the core source path still reaches an exact USAspending award and that the named role
contact still appears within one record on the awardee's allowlisted official website.
It never writes to Slack, Salesforce, email, the database, or the filesystem.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Sequence
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .models import RawItem
from .sources.base import polite_get
from .sources.usaspending import MAX_PAGES, _query_page, parse_awards

TARGET_AWARD_ID = "15JCOPS25GG01291SSIX"
TARGET_ENTITY = "BIRMINGHAM COMMUNITY CHARTER HIGH SCHOOL"
TARGET_AMOUNT = 500_000.0
TARGET_START = "2025-10-01"
TARGET_END = "2028-09-30"
TARGET_CFSA = "16.071"
TARGET_STATE = "CA"
OFFICIAL_STAFF_URL = "https://www.bcchs.net/dream-it-do-it/staff-directory"
OFFICIAL_STAFF_HOST = "www.bcchs.net"
TARGET_CONTACT = "Vic Chalabian"
TARGET_TITLE = "IT Systems Manager"


@dataclass(frozen=True)
class AwardEvidence:
    """Exact live award fields observed through the production source parser."""

    award_id: str
    entity: str
    amount: float
    spend_start: str
    spend_end: str
    source_url: str


@dataclass(frozen=True)
class ContactEvidence:
    """A name and role proven to coexist in one official directory record."""

    name: str
    title: str
    official_url: str
    association: str


@dataclass(frozen=True)
class LiveVerificationReport:
    """Machine-readable result of the two bounded, read-only live checks."""

    label: str
    award: AwardEvidence
    contact: ContactEvidence
    limitations: tuple[str, ...]


def _assert_exact_https_host(url: str, expected_host: str) -> None:
    """Reject redirects or configuration drift outside an exact HTTPS allowlist."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        raise RuntimeError(f"refusing non-allowlisted live URL: {url}")


def _find_exact_award(items: Sequence[RawItem]) -> AwardEvidence:
    """Return the immutable golden award only when all expected fields still match."""
    matches = [item for item in items if item.item_id == TARGET_AWARD_ID]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {TARGET_AWARD_ID} award; observed {len(matches)}"
        )
    item = matches[0]
    amount = float(item.amount or 0)
    observed = (item.entity.upper(), amount, item.start, item.end)
    expected = (TARGET_ENTITY, TARGET_AMOUNT, TARGET_START, TARGET_END)
    if observed != expected:
        raise RuntimeError(
            f"award fields drifted: expected {expected!r}; observed {observed!r}"
        )
    _assert_exact_https_host(item.url, "www.usaspending.gov")
    return AwardEvidence(
        award_id=item.item_id,
        entity=item.entity,
        amount=amount,
        spend_start=item.start,
        spend_end=item.end,
        source_url=item.url,
    )


def verify_award_live() -> AwardEvidence:
    """Query bounded USAspending pages and verify the exact Birmingham award."""
    items: list[RawItem] = []
    for page in range(1, MAX_PAGES + 1):
        payload = _query_page(TARGET_CFSA, TARGET_STATE, page)
        items.extend(parse_awards(payload, TARGET_CFSA, TARGET_STATE))
        metadata = payload.get("page_metadata") or {}
        if not bool(metadata.get("hasNext")):
            break
    return _find_exact_award(items)


def _find_contact_record(html: str) -> ContactEvidence:
    """Require the target name and title inside the same official directory card."""
    soup = BeautifulSoup(html, "html.parser")
    matching_records = []
    for record in soup.select("div.fsConstituentItem"):
        name_node = record.select_one(".fsFullName")
        title_node = record.select_one(".fsTitles")
        name = (
            " ".join(name_node.get_text(" ", strip=True).split()) if name_node else ""
        )
        title = (
            " ".join(title_node.get_text(" ", strip=True).split()) if title_node else ""
        )
        if name == TARGET_CONTACT and TARGET_TITLE in title:
            matching_records.append((name, TARGET_TITLE))
    if len(matching_records) != 1:
        raise RuntimeError(
            "expected one same-record official contact match; "
            f"observed {len(matching_records)}"
        )
    name, title = matching_records[0]
    return ContactEvidence(
        name=name,
        title=title,
        official_url=OFFICIAL_STAFF_URL,
        association="same official directory record",
    )


def verify_contact_live() -> ContactEvidence:
    """Fetch only the allowlisted school directory and verify same-record identity."""
    _assert_exact_https_host(OFFICIAL_STAFF_URL, OFFICIAL_STAFF_HOST)
    response = polite_get(OFFICIAL_STAFF_URL)
    _assert_exact_https_host(response.url, OFFICIAL_STAFF_HOST)
    return _find_contact_record(response.text)


def run_live_verification() -> LiveVerificationReport:
    """Run the bounded read-only checks and disclose what they do not prove."""
    return LiveVerificationReport(
        label="verified",
        award=verify_award_live(),
        contact=verify_contact_live(),
        limitations=(
            "The USAspending endpoint does not provide an award announcement date.",
            "The directory verifies the public role, not a personal email address.",
            "No LinkedIn profile ownership, Salesforce state, or outreach send is verified.",
        ),
    )


def _live_execution_allowed(execute_live: bool) -> bool:
    """Require two explicit local opt-ins and reject accidental CI execution."""
    return (
        execute_live
        and os.environ.get("GRANT_LIVE_VERIFICATION") == "1"
        and not os.environ.get("CI")
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the permanent live verifier only behind its explicit safety gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="perform the two allowlisted read-only network requests",
    )
    args = parser.parse_args(argv)
    if not _live_execution_allowed(bool(args.execute_live)):
        parser.error("set GRANT_LIVE_VERIFICATION=1 and pass --execute-live outside CI")
    report = run_live_verification()
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
