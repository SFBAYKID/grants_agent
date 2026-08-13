"""SAM.gov Opportunities poller — official physical-security solicitations.

VERIFICATION: verified live 2026-07-13 with Chase's key — returned 4 real WA security
records (security fencing, security cameras at JBLM, etc.). Runtime promotion is
stricter than the search response: an item must be an active solicitation, carry a
current deadline, match physical-security language, and agree with the requested
place-of-performance state when SAM supplies one.
Requires SAM_API_KEY in .env; poller is skipped when the key is absent.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any  # SAM.gov API response JSON is runtime-shaped.
from urllib.parse import parse_qs, unquote, urlsplit

import requests

from ..models import DatePrecision, FundingEventType, RawItem, VerificationStatus
from ..state_codes import normalize_state_code
from .base import polite_get
from .usaspending import watch_states

API_URL = "https://api.sam.gov/prod/opportunities/v2/search"
PAGE_LIMIT = 1000
MAX_PAGES = 100
_SOLICITATION_TYPES = frozenset(
    {"solicitation", "combined synopsis/solicitation", "o", "k"}
)
_PHYSICAL_SECURITY_RE = re.compile(
    r"\b(?:cctv|surveillance|security cameras?|video security|access control|"
    r"security gates?|security fencing|perimeter fencing|intrusion detection|"
    r"alarm systems?|door hardware|physical security)\b",
    re.IGNORECASE,
)
_CYBER_ONLY_RE = re.compile(
    r"\b(?:cyber(?:security)?|information security|network security|infosec|"
    r"zero trust|endpoint security)\b",
    re.IGNORECASE,
)
_GUARD_SERVICE_RE = re.compile(
    r"\b(?:armed|unarmed|security) guards?\b|\bguard services?\b", re.IGNORECASE
)


def _active(value: object) -> bool:
    """Recognize only SAM values that explicitly assert an active notice."""
    if value is True:
        return True
    return str(value or "").strip().lower() in {"yes", "true", "active", "1"}


def _deadline(value: object) -> date | None:
    """Parse the ISO date portion of a SAM response deadline."""
    raw = str(value or "").strip()
    if len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _place_state(opp: dict[str, Any]) -> str:
    """Return SAM's explicit place-of-performance code, if present and valid."""
    place = opp.get("placeOfPerformance")
    if not isinstance(place, dict):
        return ""
    state = place.get("state")
    if not isinstance(state, dict):
        return ""
    try:
        return normalize_state_code(str(state.get("code") or ""))
    except ValueError:
        return ""


def _public_ui_link(value: object, notice_id: str) -> str:
    """Accept only an exact official SAM URL bound to this notice identifier."""
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "sam.gov"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        return ""
    tokens = {unquote(part) for part in parsed.path.split("/") if part}
    for values in parse_qs(parsed.query).values():
        tokens.update(values)
    return raw if notice_id in tokens else ""


def parse_opportunities(
    payload: dict[str, Any], requested_state: str, today: date | None = None
) -> list[RawItem]:
    """Promote valid active solicitations from one state-scoped search page."""
    state = normalize_state_code(requested_state)
    current = today or date.today()
    out: list[RawItem] = []
    for opp in payload.get("opportunitiesData", []):
        if not isinstance(opp, dict):
            continue
        notice_id = str(opp.get("noticeId") or "").strip()
        notice_type = str(opp.get("type") or "").strip().lower()
        title = str(opp.get("title") or "").strip()
        entity = str(opp.get("fullParentPathName") or "").strip()
        due = _deadline(opp.get("responseDeadLine"))
        explicit_state = _place_state(opp)
        public_url = _public_ui_link(opp.get("uiLink"), notice_id)
        if (
            not notice_id
            or notice_type not in _SOLICITATION_TYPES
            or not _active(opp.get("active"))
            or due is None
            or due <= current
            or not title
            or not entity
            or _PHYSICAL_SECURITY_RE.search(title) is None
            or _CYBER_ONLY_RE.search(title) is not None
            or _GUARD_SERVICE_RE.search(title) is not None
            or explicit_state != state
            or not public_url
        ):
            continue
        out.append(
            RawItem(
                source="sam.gov",
                item_id=notice_id,
                title=title,
                entity=entity,
                state=explicit_state,
                program="RFP:sam.gov",
                amount=None,
                start=opp.get("postedDate") or "",
                end=opp.get("responseDeadLine") or "",
                url=public_url,
                raw={
                    "noticeId": notice_id,
                    "postedDate": opp.get("postedDate"),
                    "type": opp.get("type"),
                    "active": opp.get("active"),
                    "requested_state": state,
                    "place_of_performance_state": explicit_state,
                },
                event_type=FundingEventType.RFP_POSTED,
                event_date=(opp.get("postedDate") or "")[:10],
                date_precision=DatePrecision.DAY,
                application_portal="SAM.gov",
                source_locator=notice_id,
                evidence_excerpt=(
                    f"{title}; {opp.get('type')}; responses due "
                    f"{str(opp.get('responseDeadLine') or '')[:10]}"
                )[:500],
                verification_status=VerificationStatus.VERIFIED,
            )
        )
    return out


def poll(
    api_key: str,
    states: tuple[str, ...] | None = None,
    today: date | None = None,
) -> list[RawItem]:
    """Fetch every page for each validated configured state and deduplicate notices."""
    current = today or date.today()
    requested_states = states or watch_states()
    by_notice: dict[str, RawItem] = {}
    ambiguous: set[str] = set()
    for requested_state in requested_states:
        state = normalize_state_code(requested_state)
        records_seen = 0
        for offset in range(MAX_PAGES):
            try:
                response = polite_get(
                    API_URL,
                    {
                        "api_key": api_key,
                        "limit": PAGE_LIMIT,
                        "offset": offset,
                        "postedFrom": current.replace(day=1).strftime("%m/%d/%Y"),
                        "postedTo": current.strftime("%m/%d/%Y"),
                        "state": state,
                        "title": "security",
                        "ptype": "o,k",
                    },
                )
            except requests.HTTPError as exc:
                if (
                    exc.response is not None
                    and exc.response.status_code == 404
                    and offset == 0
                ):
                    break
                raise
            payload = response.json()
            records = payload.get("opportunitiesData")
            total = payload.get("totalRecords")
            if not isinstance(records, list) or not isinstance(total, int):
                raise ValueError("SAM.gov response lacks pagination metadata")
            records_seen += len(records)
            for item in parse_opportunities(payload, state, current):
                prior = by_notice.get(item.item_id)
                if prior is not None and prior.state != item.state:
                    ambiguous.add(item.item_id)
                    by_notice.pop(item.item_id, None)
                elif item.item_id not in ambiguous:
                    by_notice[item.item_id] = item
            if records_seen >= total:
                break
            if not records:
                raise RuntimeError("SAM.gov pagination stopped before totalRecords")
        else:
            raise RuntimeError(
                f"SAM.gov pagination exceeded {MAX_PAGES} pages for {state}"
            )
    return list(by_notice.values())
