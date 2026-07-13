"""SAM.gov Opportunities poller — federal-side security RFPs (SILVER leads).

VERIFICATION: verified live 2026-07-13 with Chase's key — returned 4 real WA security
solicitations (security fencing, security cameras at JBLM, etc.). Still unverified:
rate limits, and whether text search is title-only (we currently search `title` —
works, but may miss body-only matches; investigate before widening).
Requires SAM_API_KEY in .env; poller is skipped when the key is absent.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..models import RawItem
from .base import polite_get

API_URL = "https://api.sam.gov/prod/opportunities/v2/search"


def parse_opportunities(payload: dict[str, Any]) -> list[RawItem]:
    """Pure parser for one opportunities-search response."""
    out: list[RawItem] = []
    for opp in payload.get("opportunitiesData", []):
        out.append(RawItem(
            source="sam.gov",
            item_id=str(opp.get("noticeId") or ""),
            title=opp.get("title") or "",
            entity=opp.get("fullParentPathName") or "",
            state="WA",  # we filter the query by place-of-performance state
            program="RFP:sam.gov",
            amount=None,
            start=opp.get("postedDate") or "",
            end=opp.get("responseDeadLine") or "",
            url=opp.get("uiLink") or "",
            raw={k: opp.get(k) for k in ("noticeId", "postedDate", "type")},
        ))
    return out


def poll(api_key: str) -> list[RawItem]:
    """Search this month's WA 'security' opportunities. Key is mandatory (verified:
    keyless requests are rejected)."""
    payload = polite_get(API_URL, {
        "api_key": api_key,
        "limit": 100,
        "postedFrom": date.today().replace(day=1).strftime("%m/%d/%Y"),
        "postedTo": date.today().strftime("%m/%d/%Y"),
        "state": "WA",
        "title": "security",
    }).json()
    return parse_opportunities(payload)
