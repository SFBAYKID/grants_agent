"""NCES 2024-25 district enrollment/location enrichment via official ArcGIS APIs.

Why: Grant must answer enrollment-filtered school questions without pretending the
lead sources contain enrollment. NCES publishes school-level membership plus district
office locations without an API key. This module aggregates membership by LEA and
matches only a unique, conservatively normalized district name within one state.

Verification: API fields and a Tustin Unified aggregate were verified live 2026-07-14.
Parser/matching tests are offline; production-wide matching remains needs-testing.
"""

from __future__ import annotations

import json
import ipaddress
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..sources.base import polite_get
from ..state_codes import normalize_state_code

SCHOOL_QUERY_URL = (
    "https://nces.ed.gov/opengis/rest/services/K12_School_Locations/"
    "EDGE_ADMINDATA_PUBLICSCH_2425/MapServer/1/query"
)
LEA_QUERY_URL = (
    "https://nces.ed.gov/opengis/rest/services/K12_School_Locations/"
    "EDGE_GEOCODE_PUBLICLEA_2425/MapServer/0/query"
)
SOURCE_URL = (
    "https://nces.ed.gov/opengis/rest/services/K12_School_Locations/"
    "EDGE_ADMINDATA_PUBLICSCH_2425/MapServer/1"
)
DISTRICT_DETAIL_URL = "https://nces.ed.gov/ccd/districtsearch/district_detail.asp"
PAGE_SIZE = 2_000
MAX_PAGES = 100
_GENERIC = {
    "school",
    "schools",
    "district",
    "public",
    "unified",
    "union",
    "elementary",
    "secondary",
    "high",
    "community",
    "consolidated",
    "independent",
    "local",
    "education",
    "educational",
    "agency",
    "sd",
    "usd",
    "isd",
    "lea",
}


@dataclass(frozen=True)
class NCESDistrict:
    """One district's official identifier, name, location, and aggregated membership."""

    nces_id: str
    name: str
    state: str
    city: str
    enrollment: int
    source_url: str = SOURCE_URL


@dataclass(frozen=True)
class EnrichmentSummary:
    """Coverage counts from one state-level enrichment pass."""

    candidates: int
    matched: int
    ambiguous_or_unmatched: int
    websites_verified: int = 0
    website_unavailable: int = 0


@dataclass(frozen=True)
class NCESWebsiteEvidence:
    """Website claim read from one exact NCES district-id detail record."""

    website: str
    source_url: str
    status: str


def normalize_name(name: str) -> str:
    """Normalize district naming variants without broad fuzzy identity inference."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join(token for token in tokens if token not in _GENERIC)


def _features(payload: dict[str, object]) -> list[dict[str, object]]:
    """Return ArcGIS feature attribute maps or fail loudly on an API error."""
    if isinstance(payload.get("error"), dict):
        error = cast(dict[str, object], payload["error"])
        raise ValueError(f"NCES ArcGIS error {error.get('code', 'unknown')}")
    raw_features = payload.get("features")
    if not isinstance(raw_features, list):
        raise ValueError("NCES response has no feature list")
    attributes: list[dict[str, object]] = []
    for feature in raw_features:
        if isinstance(feature, dict) and isinstance(feature.get("attributes"), dict):
            attributes.append(cast(dict[str, object], feature["attributes"]))
    return attributes


def parse_districts(
    enrollment_payload: dict[str, object], location_payload: dict[str, object]
) -> list[NCESDistrict]:
    """Merge school enrollment aggregates with LEA office cities by NCES ID."""
    cities = {
        str(item.get("LEAID") or "").strip(): str(item.get("CITY") or "").strip()
        for item in _features(location_payload)
        if str(item.get("LEAID") or "").strip()
    }
    districts: list[NCESDistrict] = []
    for item in _features(enrollment_payload):
        nces_id = str(item.get("LEAID") or "").strip()
        name = str(item.get("LEA_NAME") or "").strip()
        state = str(item.get("LSTATE") or "").strip().upper()
        try:
            enrollment = int(round(float(item.get("ENROLLMENT") or 0)))
        except (TypeError, ValueError):
            continue
        if nces_id and name and state and enrollment >= 0:
            districts.append(
                NCESDistrict(nces_id, name, state, cities.get(nces_id, ""), enrollment)
            )
    return districts


def match_district(
    entity_name: str, districts: list[NCESDistrict]
) -> NCESDistrict | None:
    """Return only a unique exact normalized-name match; ambiguity is no match."""
    key = normalize_name(entity_name)
    if not key:
        return None
    matches = [
        district for district in districts if normalize_name(district.name) == key
    ]
    return matches[0] if len(matches) == 1 else None


def _safe_published_website(value: str) -> str:
    """Normalize an NCES-published organization site to a public HTTPS URL."""
    raw = value.strip()
    if not raw or raw.upper() in {"N", "N/A", "NOT AVAILABLE"}:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").strip(".").lower()
    try:
        ipaddress.ip_address(hostname)
        is_ip_address = True
    except ValueError:
        is_ip_address = False
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or "." not in hostname
        or is_ip_address
        or parsed.username is not None
        or parsed.password is not None
        or any(char in raw for char in ("<", ">", "|", "\n", "\r"))
    ):
        return ""
    # Slack's link boundary accepts HTTPS only. NCES still prints many legacy links
    # as HTTP, but the exact published host remains the identity evidence.
    netloc = hostname + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit(("https", netloc, parsed.path, parsed.query, ""))


def _exact_detail_source(url: str, nces_id: str) -> bool:
    """Require the canonical NCES detail endpoint and this exact ID2 query value."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "nces.ed.gov"
        and parsed.path.lower() == "/ccd/districtsearch/district_detail.asp"
        and parse_qs(parsed.query).get("ID2") == [nces_id]
    )


def parse_official_website(html: str, nces_id: str) -> str:
    """Extract a website only from a detail page bound to the requested NCES ID."""
    if not re.fullmatch(r"\d{7}", nces_id):
        raise ValueError("an NCES district ID must contain exactly seven digits")
    soup = BeautifulSoup(html, "html.parser")
    page_text = " ".join(soup.stripped_strings)
    if not re.search(rf"NCES District ID:\s*{re.escape(nces_id)}\b", page_text):
        raise ValueError("NCES detail page did not confirm the requested district ID")
    label = soup.find(string=re.compile(r"^\s*Website:\s*$", re.IGNORECASE))
    parent = label.parent.parent if label is not None and label.parent else None
    anchor = parent.find("a", href=True) if parent is not None else None
    if anchor is None:
        return ""
    candidate = anchor.get_text(" ", strip=True)
    if not candidate:
        query = parse_qs(urlsplit(str(anchor.get("href") or "")).query)
        candidate = str((query.get("location") or [""])[0])
    return _safe_published_website(candidate)


def fetch_official_website(nces_id: str) -> NCESWebsiteEvidence:
    """Read the official site from one exact, public NCES district detail page."""
    if not re.fullmatch(r"\d{7}", nces_id):
        raise ValueError("an NCES district ID must contain exactly seven digits")
    source_url = f"{DISTRICT_DETAIL_URL}?ID2={nces_id}"
    response = polite_get(DISTRICT_DETAIL_URL, params={"ID2": nces_id})
    website = parse_official_website(response.text, nces_id)
    return NCESWebsiteEvidence(
        website=website,
        source_url=source_url,
        status="verified" if website else "not_found",
    )


def _fetch_all_features(url: str, base_params: dict[str, str]) -> dict[str, object]:
    """Page an ArcGIS query fully and fail closed if pagination does not advance.

    PAGES BY KEY RANGE, NOT BY `resultOffset`, because this service silently ignores
    the offset on a `groupByFieldsForStatistics` aggregate. Measured live against
    California on 2026-08-13: `resultOffset=0` and `resultOffset=2000` returned the
    IDENTICAL 2,000 rows — same first LEAID (0600001) and same last (0691046) — with
    `exceededTransferLimit=True` on both, so real rows existed and were unreachable.
    The old loop advanced its offset, got the same page, and correctly refused; the
    guard was right and the mechanism underneath it was wrong. California, the largest
    state, was the only one whose grouped output exceeds one page, which is why the
    whole `nces-bind` run for it aborted while every other state passed.

    Advancing on `LEAID > <last seen>` reaches the rest: page 2 began at 0691047 and
    returned the final 38 rows, 2,038 in total.

    THE CURSOR FIELD MUST BE UNIQUE PER ROW, which is why it is taken from
    `orderByFields` rather than passed separately — a cursor that is not the sort key
    would skip rows silently. Both callers group or key on LEAID, where it is unique.
    """
    cursor_field = str(base_params.get("orderByFields", "")).strip()
    if not cursor_field or "," in cursor_field:
        raise ValueError("NCES paging needs exactly one orderByFields cursor column")
    base_where = str(base_params.get("where", "")).strip()
    collected: list[dict[str, object]] = []
    cursor = ""
    for _page in range(MAX_PAGES):
        where = base_where
        if cursor:
            # Values come from the service's own response, never from user input, and
            # are additionally constrained to be quote-free before interpolation.
            where = f"{base_where} AND {cursor_field}>'{cursor}'"
        params = {
            **base_params,
            "where": where,
            "resultRecordCount": str(PAGE_SIZE),
        }
        payload = cast(dict[str, object], polite_get(url, params=params).json())
        attributes = _features(payload)
        if not attributes:
            return {"features": [{"attributes": item} for item in collected]}
        collected.extend(attributes)
        last = str(attributes[-1].get(cursor_field, "") or "")
        if not last or "'" in last:
            raise ValueError(f"NCES returned an unusable {cursor_field} cursor value")
        if last == cursor:
            raise ValueError("NCES pagination repeated a page without advancing")
        cursor = last
        more = (
            payload.get("exceededTransferLimit") is True or len(attributes) >= PAGE_SIZE
        )
        if not more:
            return {"features": [{"attributes": item} for item in collected]}
    raise ValueError(f"NCES pagination exceeded {MAX_PAGES} pages")


def fetch_state(state: str) -> list[NCESDistrict]:
    """Fetch and merge one state's current district membership/location data."""
    state_code = normalize_state_code(state)
    enrollment_params = {
        # NCES negative MEMBER values are missing/not-applicable sentinels, not pupils.
        "where": f"LSTATE='{state_code}' AND MEMBER>=0",
        "outStatistics": json.dumps(
            [
                {
                    "statisticType": "sum",
                    "onStatisticField": "MEMBER",
                    "outStatisticFieldName": "ENROLLMENT",
                }
            ]
        ),
        "groupByFieldsForStatistics": "LEAID,LEA_NAME,LSTATE",
        "outFields": "LEAID,LEA_NAME,LSTATE",
        "orderByFields": "LEAID",
        "returnGeometry": "false",
        "f": "json",
    }
    location_params = {
        "where": f"STATE='{state_code}'",
        "outFields": "LEAID,NAME,STATE,CITY",
        "orderByFields": "LEAID",
        "returnGeometry": "false",
        "f": "json",
    }
    enrollments = _fetch_all_features(SCHOOL_QUERY_URL, enrollment_params)
    locations = _fetch_all_features(LEA_QUERY_URL, location_params)
    return parse_districts(enrollments, locations)


def enrich_state_leads(
    conn: sqlite3.Connection,
    state: str,
    districts: list[NCESDistrict] | None = None,
    *,
    website_fetcher: Callable[[str], NCESWebsiteEvidence] | None = None,
) -> EnrichmentSummary:
    """Attach exact NCES identity, facts, and published website for one state."""
    state_code = normalize_state_code(state)
    supplied = districts if districts is not None else fetch_state(state_code)
    reference = [district for district in supplied if district.state == state_code]
    by_id = {district.nces_id: district for district in reference}
    lookup_website = (
        website_fetcher
        if website_fetcher is not None
        else fetch_official_website
        if districts is None
        else None
    )
    rows = list(
        conn.execute(
            """SELECT id,entity_name,nces_id,nces_website_status FROM leads
           WHERE UPPER(state)=?
             AND ((nces_id IS NULL OR nces_id='')
                  OR COALESCE(nces_website_status,'') IN ('','unavailable'))
             AND (LOWER(COALESCE(entity_type,'')) IN
                    ('school','district','school_district','nonpublic_school')
                  OR UPPER(entity_name) LIKE '%SCHOOL%'
                  OR UPPER(entity_name) LIKE '%DISTRICT%'
                  OR UPPER(entity_name) LIKE '% USD'
                  OR UPPER(entity_name) LIKE '% ISD')""",
            (state_code,),
        )
    )
    matched = websites_verified = website_unavailable = 0
    checked_at = datetime.now(timezone.utc).isoformat()
    with conn:
        for row in rows:
            stored_id = str(row["nces_id"] or "").strip()
            district = by_id.get(stored_id) if stored_id else None
            if district is None:
                district = match_district(str(row["entity_name"]), reference)
            if district is None:
                continue
            website = ""
            website_source = f"{DISTRICT_DETAIL_URL}?ID2={district.nces_id}"
            website_status = str(row["nces_website_status"] or "")
            if lookup_website is not None:
                try:
                    evidence = lookup_website(district.nces_id)
                    if not _exact_detail_source(evidence.source_url, district.nces_id):
                        raise ValueError("NCES website evidence is not exact-ID bound")
                    if evidence.status not in {"verified", "not_found"}:
                        raise ValueError("NCES website evidence has an unknown status")
                    if evidence.status == "verified" and (
                        not evidence.website
                        or _safe_published_website(evidence.website) != evidence.website
                    ):
                        raise ValueError("NCES website evidence contains an unsafe URL")
                    if evidence.status == "not_found" and evidence.website:
                        raise ValueError(
                            "NCES not-found evidence cannot carry a website"
                        )
                    website = evidence.website
                    website_source = evidence.source_url
                    website_status = evidence.status
                    websites_verified += int(evidence.status == "verified")
                except Exception:  # noqa: BLE001 - identity data remains usable
                    website_status = "unavailable"
                    website_unavailable += 1
            conn.execute(
                """UPDATE leads SET nces_id=?,enrollment=?,location_city=?,
                          location_confidence='high',
                          nces_website=CASE WHEN ?!='' THEN ? ELSE nces_website END,
                          nces_website_source_url=?,nces_website_status=?,
                          nces_website_checked_at=? WHERE id=?""",
                (
                    district.nces_id,
                    district.enrollment,
                    district.city or None,
                    website,
                    website,
                    website_source,
                    website_status or None,
                    checked_at,
                    int(row["id"]),
                ),
            )
            matched += 1
    return EnrichmentSummary(
        len(rows),
        matched,
        len(rows) - matched,
        websites_verified,
        website_unavailable,
    )
