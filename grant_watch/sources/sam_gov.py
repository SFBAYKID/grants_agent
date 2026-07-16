"""SAM.gov Opportunities poller — federal-side security RFPs (SILVER leads).

VERIFICATION: verified live 2026-07-13 with Chase's key — returned 4 real WA security
solicitations (security fencing, security cameras at JBLM, etc.). Still unverified:
rate limits, and whether text search is title-only (we currently search `title` —
works, but may miss body-only matches; investigate before widening).
Requires SAM_API_KEY in .env; poller is skipped when the key is absent.
"""

from __future__ import annotations

from datetime import date
from typing import Any  # SAM.gov API response JSON is runtime-shaped.

import requests

from ..models import DatePrecision, FundingEventType, RawItem, VerificationStatus
from .base import polite_get

API_URL = "https://api.sam.gov/prod/opportunities/v2/search"
PAGE_LIMIT = 1000
MAX_PAGES = 100


def parse_opportunities(payload: dict[str, Any]) -> list[RawItem]:
    """Pure parser for one opportunities-search response."""
    out: list[RawItem] = []
    for opp in payload.get("opportunitiesData", []):
        notice_id = str(opp.get("noticeId") or "").strip()
        if not notice_id:
            continue
        out.append(
            RawItem(
                source="sam.gov",
                item_id=notice_id,
                title=opp.get("title") or "",
                entity=opp.get("fullParentPathName") or "",
                state="WA",  # we filter the query by place-of-performance state
                program="RFP:sam.gov",
                amount=None,
                start=opp.get("postedDate") or "",
                end=opp.get("responseDeadLine") or "",
                url=opp.get("uiLink") or "",
                raw={k: opp.get(k) for k in ("noticeId", "postedDate", "type")},
                event_type=FundingEventType.RFP_POSTED,
                event_date=(opp.get("postedDate") or "")[:10],
                date_precision=DatePrecision.DAY,
                application_portal="SAM.gov",
                source_locator=str(opp.get("noticeId") or ""),
                evidence_excerpt=(opp.get("title") or "")[:500],
                verification_status=VerificationStatus.VERIFIED,
            )
        )
    return out


def poll(api_key: str) -> list[RawItem]:
    """Fetch every page of this month's WA security opportunities."""
    out: list[RawItem] = []
    for offset in range(MAX_PAGES):
        try:
            response = polite_get(
                API_URL,
                {
                    "api_key": api_key,
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                    "postedFrom": date.today().replace(day=1).strftime("%m/%d/%Y"),
                    "postedTo": date.today().strftime("%m/%d/%Y"),
                    "state": "WA",
                    "title": "security",
                },
            )
        except requests.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 404
                and offset == 0
            ):
                return []
            raise
        payload = response.json()
        records = payload.get("opportunitiesData")
        total = payload.get("totalRecords")
        if not isinstance(records, list) or not isinstance(total, int):
            raise ValueError("SAM.gov response lacks pagination metadata")
        out.extend(parse_opportunities(payload))
        if len(out) >= total:
            return out
        if not records:
            raise RuntimeError("SAM.gov pagination stopped before totalRecords")
    raise RuntimeError(f"SAM.gov pagination exceeded {MAX_PAGES} pages")
