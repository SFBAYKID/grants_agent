"""USASpending poller — districts/cities that WON security money (the real GOLD source).

VERIFICATION: verified live 2026-07-13 (returned Castle Rock SD $500K, Nespelem SD, and
100+ 16.710 rows). Two fixes over the v1 scaffold, both from that first live run:
  1. SVPP FILTER — 16.710 is the whole COPS umbrella (police hiring, tribal equipment...).
     Only rows whose description matches _SVPP_RE are SVPP. Querying 16.710 unfiltered
     produced ~99 non-school rows out of 100 (docs/FINDINGS.md gotcha, now enforced).
  2. PAGINATION — the API caps at 100 rows/page; v1 silently truncated. We follow
     page_metadata.hasNext.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..models import RawItem
from .base import polite_post

API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# SVPP is split across two assistance listings — VERIFIED live (docs/FINDINGS.md):
#   16.071 = SVPP-specific listing (FY25+ awards)
#   16.710 = COPS umbrella (FY21–FY24 SVPP lives here, among 450+ unrelated awards)
SVPP_CFDAS = ("16.071", "16.710")
_SVPP_RE = re.compile(r"school violence|SVPP", re.IGNORECASE)

# States we watch today; expansion is config, not code (CLAUDE.md mission #6).
WATCH_STATES = ("WA", "CA", "MI", "PA")

TIME_FLOOR = "2018-10-01"  # keep queries bounded; freshness scoring discards old anyway
PAGE_LIMIT = 100           # API max page size — verified live
MAX_PAGES = 20             # runaway guard; 2000 rows far exceeds any real result set


def _query_page(cfda: str, state: str, page: int) -> dict[str, Any]:
    """One page of grant awards for one CFDA in one state. Payload shape is an exact
    copy of the browser-verified call (docs/FINDINGS.md)."""
    resp = polite_post(API_URL, {
        "filters": {
            "award_type_codes": ["02", "03", "04", "05"],  # grants
            "program_numbers": [cfda],
            "recipient_locations": [{"country": "USA", "state": state}],
            "time_period": [{"start_date": TIME_FLOOR,
                             "end_date": date.today().isoformat()}],
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount",
                   "Start Date", "End Date", "Description", "generated_internal_id"],
        "limit": PAGE_LIMIT,
        "page": page,
        "subawards": False,
    })
    return resp.json()


def parse_awards(payload: dict[str, Any], cfda: str, state: str) -> list[RawItem]:
    """Pure parser for one response page. 16.710 rows must pass the SVPP regex;
    16.071 is SVPP-only by definition so no filter is needed."""
    out: list[RawItem] = []
    for a in payload.get("results", []):
        desc: str = a.get("Description") or ""
        if cfda == "16.710" and not _SVPP_RE.search(desc):
            continue  # COPS umbrella noise (CHP hiring, TRGP, ...) — not school security
        gid: str = a.get("generated_internal_id") or ""
        out.append(RawItem(
            source=f"usaspending:{cfda}",
            item_id=str(a.get("Award ID") or gid),
            title=desc[:160],
            entity=a.get("Recipient Name") or "",
            state=state,
            program="SVPP",
            amount=a.get("Award Amount"),
            start=a.get("Start Date") or "",
            end=a.get("End Date") or "",
            url=f"https://www.usaspending.gov/award/{gid}" if gid else "",
            raw={k: a.get(k) for k in ("Award ID", "Award Amount", "Start Date",
                                       "End Date", "generated_internal_id")},
        ))
    return out


def poll() -> list[RawItem]:
    """Fetch all SVPP awards across both CFDAs and all watched states, paginating."""
    out: list[RawItem] = []
    for cfda in SVPP_CFDAS:
        for state in WATCH_STATES:
            for page in range(1, MAX_PAGES + 1):
                payload = _query_page(cfda, state, page)
                out.extend(parse_awards(payload, cfda, state))
                if not payload.get("page_metadata", {}).get("hasNext"):
                    break
    return out
