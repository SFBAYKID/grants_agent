"""WEBS (Washington Electronic Business Solution) bid-calendar scraper — SILVER leads.

VERIFICATION: needs-testing. The page is public (verified live: fetch succeeds, no
login) and the parser ran without error on 2026-07-13, but it returned 0 keyword
matches that day — identical to the browser session's finding of 0 visible security
rows (docs/FINDINGS.md), so we cannot yet distinguish "no security bids this week"
from "selectors miss collapsed rows". Revisit when a known security bid is live, and
capture a fixture that contains one.

Structure notes from FINDINGS: 2000s ASP.NET frameset; parse RAW HTML because collapsed
rows are invisible to innerText; the ORG NAME lives in group-header rows above bid rows.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import DatePrecision, FundingEventType, RawItem, VerificationStatus
from .base import polite_get

URL = "https://pr-webs-vendor.des.wa.gov/BidCalendar.aspx"

_KEYWORD_RE = re.compile(
    r"camera|surveillance|access control|security|cctv|video|intrusion|alarm"
    r"|door hardware", re.IGNORECASE)
_REF_RE = re.compile(r"Ref\s*#?:\s*(\S+)")


def parse_bid_calendar(html: str) -> list[RawItem]:
    """Pure parser: scan every <tr> in the raw HTML for security keywords.

    First-pass heuristic (row-level scan). The org-name-in-group-header refinement is
    deliberately deferred until we have a fixture with a real security bid to test
    against — guessing selectors would violate the no-fabrication rule.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawItem] = []
    for tr in soup.find_all("tr"):
        text = " ".join(tr.get_text(" ", strip=True).split())
        match = _KEYWORD_RE.search(text) if text else None
        if not match:
            continue
        ref = _REF_RE.search(text)
        out.append(RawItem(
            source="webs",
            item_id=ref.group(1) if ref else text[:80],
            title=text[:200],
            entity="",  # lives in a group-header row; refine with a real fixture
            state="WA",
            program="RFP:webs",
            amount=None,
            start="",
            end="",
            url=URL,
            raw={"matched_keyword": match.group(0), "row_text": text[:500]},
            event_type=FundingEventType.RFP_POSTED,
            event_date="",
            date_precision=DatePrecision.UNKNOWN,
            application_portal="WEBS",
            source_locator=ref.group(1) if ref else text[:80],
            evidence_excerpt=text[:500],
            verification_status=VerificationStatus.NEEDS_TESTING,
        ))
    return out


def poll() -> list[RawItem]:
    """Fetch the public bid calendar and parse it."""
    return parse_bid_calendar(polite_get(URL).text)
