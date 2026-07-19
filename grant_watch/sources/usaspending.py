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

import os
import re
from datetime import date, timedelta
from typing import Any  # USAspending API response JSON is runtime-shaped.

from ..models import (
    DatePrecision,
    FundingEventType,
    RawItem,
    VerificationStatus,
)
from .base import polite_post

API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# SVPP is split across two assistance listings — VERIFIED live (docs/FINDINGS.md):
#   16.071 = SVPP-specific listing (FY25+ awards)
#   16.710 = COPS umbrella (FY21–FY24 SVPP lives here, among 450+ unrelated awards)
SVPP_CFDAS = ("16.071", "16.710")
_SVPP_RE = re.compile(r"school violence|SVPP", re.IGNORECASE)

# Nationwide by default; GRANT_WATCH_STATES can narrow a local/test run without code
# changes (for example "CA,OR,WA"). DC is included; territories can be configured.
ALL_STATES = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
)
NSGP_CFDA = "97.008"

TIME_FLOOR = "2018-10-01"  # keep queries bounded; freshness scoring discards old anyway
PAGE_LIMIT = 100  # API max page size — verified live
MAX_PAGES = 20  # runaway guard; 2000 rows far exceeds any real result set


def watch_states() -> tuple[str, ...]:
    """Return validated configured state codes, defaulting to all states plus DC."""
    raw = os.environ.get("GRANT_WATCH_STATES", "").strip()
    if not raw:
        return ALL_STATES
    states = tuple(
        dict.fromkeys(part.strip().upper() for part in raw.split(",") if part.strip())
    )
    invalid = [state for state in states if not re.fullmatch(r"[A-Z]{2}", state)]
    if invalid:
        raise ValueError(f"invalid GRANT_WATCH_STATES codes: {', '.join(invalid)}")
    return states


def _query_page(
    cfda: str, state: str, page: int, subawards: bool = False
) -> dict[str, Any]:
    """One page of grant awards for one CFDA in one state. Payload shape is an exact
    copy of the browser-verified call (docs/FINDINGS.md)."""
    resp = polite_post(
        API_URL,
        {
            "filters": {
                "award_type_codes": ["02", "03", "04", "05"],  # grants
                "program_numbers": [cfda],
                "recipient_locations": [{"country": "USA", "state": state}],
                "time_period": [
                    {"start_date": TIME_FLOOR, "end_date": date.today().isoformat()}
                ],
            },
            "fields": (
                [
                    "Sub-Award ID",
                    "Sub-Awardee Name",
                    "Sub-Award Amount",
                    "Sub-Award Date",
                    "Sub-Award Description",
                    "prime_award_generated_internal_id",
                ]
                if subawards
                else [
                    "Award ID",
                    "Recipient Name",
                    "Award Amount",
                    "Start Date",
                    "End Date",
                    # The real award-action date (when the money was obligated), distinct
                    # from Start Date (the period-of-performance start). VERIFIED live
                    # 2026-07-19: FY25 SVPP awards return Base Obligation Date 2025-10-10
                    # while Start Date is 2025-10-01. Drives freshness + the platinum tier.
                    "Base Obligation Date",
                    "Description",
                    "generated_internal_id",
                ]
            ),
            "limit": PAGE_LIMIT,
            "page": page,
            "subawards": subawards,
        },
    )
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
        # The real award-action date (money obligated). Verified live to be present and
        # distinct from Start Date; may be absent on some historical rows, so it is used
        # only when present and never guessed from the spend-window start.
        obligated: str = str(a.get("Base Obligation Date") or "")[:10]
        # Prefer the true award date for the historical/backfill cutoff; fall back to the
        # spend-window start only when the award date is missing.
        dated = obligated or (str(a.get("Start Date") or "")[:10])
        out.append(
            RawItem(
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
                raw={
                    k: a.get(k)
                    for k in (
                        "Award ID",
                        "Award Amount",
                        "Start Date",
                        "End Date",
                        "Base Obligation Date",
                        "generated_internal_id",
                    )
                },
                event_type=FundingEventType.AWARD_OBLIGATED,
                # The verified obligation date — the honest award date that drives
                # freshness and the platinum tier. Empty (never guessed) when absent.
                event_date=obligated,
                date_precision=DatePrecision.DAY if obligated else DatePrecision.UNKNOWN,
                funded_scope=desc[:500],
                eligible_scope="SVPP school security",
                source_locator=str(a.get("Award ID") or gid),
                evidence_excerpt=desc[:500],
                verification_status=VerificationStatus.VERIFIED,
                # An award obligated (or, lacking that, started) over 90 days ago is
                # historical — suppress it from first-rollout drip waves.
                backfill=bool(
                    dated and dated < (date.today() - timedelta(days=90)).isoformat()
                ),
            )
        )
    return out


def parse_nsgp_subawards(
    payload: dict[str, Any], state: str, today: date | None = None
) -> list[RawItem]:
    """Parse named NSGP end recipients with the source's explicit subaward date."""
    today = today or date.today()
    out: list[RawItem] = []
    for award in payload.get("results", []):
        subaward_id = str(award.get("Sub-Award ID") or award.get("internal_id") or "")
        parent_id = str(award.get("prime_award_generated_internal_id") or "")
        entity = str(award.get("Sub-Awardee Name") or "").strip()
        if not subaward_id or not entity:
            continue
        occurred = str(award.get("Sub-Award Date") or "")[:10]
        description = str(award.get("Sub-Award Description") or "").strip()
        out.append(
            RawItem(
                source=f"usaspending-subaward:{NSGP_CFDA}",
                item_id=f"{parent_id}:{subaward_id}" if parent_id else subaward_id,
                title=description[:160] or "NSGP subaward",
                entity=entity,
                state=state,
                program="NSGP",
                amount=award.get("Sub-Award Amount"),
                start="",
                end="",  # the endpoint does not publish a recipient spend deadline
                url=(
                    f"https://www.usaspending.gov/award/{parent_id}"
                    if parent_id
                    else ""
                ),
                raw={
                    key: award.get(key)
                    for key in (
                        "Sub-Award ID",
                        "Sub-Award Amount",
                        "Sub-Award Date",
                        "prime_award_generated_internal_id",
                    )
                },
                event_type=FundingEventType.AWARD_OBLIGATED,
                event_date=occurred,
                date_precision=DatePrecision.DAY if occurred else DatePrecision.UNKNOWN,
                funded_scope=description[:500],
                eligible_scope="NSGP nonprofit physical security",
                source_locator=subaward_id,
                evidence_excerpt=description[:500],
                verification_status=VerificationStatus.VERIFIED,
                backfill=(
                    not occurred or occurred < (today - timedelta(days=90)).isoformat()
                ),
            )
        )
    return out


def poll() -> list[RawItem]:
    """Fetch nationwide SVPP prime awards and NSGP end-recipient subawards."""
    out: list[RawItem] = []
    for state in watch_states():
        for cfda in SVPP_CFDAS:
            for page in range(1, MAX_PAGES + 1):
                payload = _query_page(cfda, state, page)
                out.extend(parse_awards(payload, cfda, state))
                if not payload.get("page_metadata", {}).get("hasNext"):
                    break
            else:
                raise RuntimeError(
                    f"USAspending pagination exceeded {MAX_PAGES} pages for {cfda}/{state}"
                )
        for page in range(1, MAX_PAGES + 1):
            payload = _query_page(NSGP_CFDA, state, page, subawards=True)
            out.extend(parse_nsgp_subawards(payload, state))
            if not payload.get("page_metadata", {}).get("hasNext"):
                break
        else:
            raise RuntimeError(
                f"USAspending pagination exceeded {MAX_PAGES} pages for {NSGP_CFDA}/{state}"
            )
    return out
