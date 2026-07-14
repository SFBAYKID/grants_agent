"""Grants.gov search2 poller — pipeline signal (opportunities open -> award wave later).

VERIFICATION: verified live 2026-07-13 (180 items on first run; no auth required).
Caveat from live testing: bare keywords like "security"/"surveillance" pull CDC disease
surveillance and cybersecurity noise, so we search exact PHRASES and still grade
everything from this source as WATCH (it is a signal, not money in hand).
"""

from __future__ import annotations

from typing import Any

from ..models import RawItem
from .base import polite_post

API_URL = "https://api.grants.gov/v1/api/search2"

# Phrase list tuned to Verkada's wheelhouse — bare single words are too noisy (FINDINGS).
KEYWORDS = (
    "school violence prevention",
    "physical security",
    "access control",
    "video surveillance",
    "security camera",
    "cctv",
    "intrusion detection",
    "visitor management",
)

ROWS_PER_QUERY = 25  # per-keyword cap; enough for a weekly diff


def _iso(us_date: str) -> str:
    """Grants.gov emits MM/DD/YYYY; store ISO so SQLite date() works (verified live
    2026-07-13: the US format silently broke every date comparison downstream)."""
    if not us_date:
        return ""
    try:
        m, d, y = us_date.split("/")
        return f"{y}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return us_date  # unknown shape: store as-is rather than invent a date


def parse_opportunities(payload: dict[str, Any], keyword: str) -> list[RawItem]:
    """Pure parser for one search2 response."""
    out: list[RawItem] = []
    for opp in payload.get("data", {}).get("oppHits", []):
        out.append(RawItem(
            source="grants.gov",
            item_id=str(opp["id"]),
            title=opp.get("title") or "",
            entity=opp.get("agency") or opp.get("agencyName") or "",
            state="",  # federal opportunities are nationwide
            program="",
            amount=None,
            start=_iso(opp.get("openDate") or ""),
            end=_iso(opp.get("closeDate") or ""),
            url=f"https://www.grants.gov/search-results-detail/{opp['id']}",
            raw={"matched_keyword": keyword, "number": opp.get("number")},
        ))
    return out


def poll() -> list[RawItem]:
    """One posted-opportunities search per keyword phrase."""
    out: list[RawItem] = []
    for kw in KEYWORDS:
        payload = polite_post(API_URL, {
            "keyword": kw, "oppStatuses": "posted", "rows": ROWS_PER_QUERY,
        }).json()
        out.extend(parse_opportunities(payload, kw))
    return out
