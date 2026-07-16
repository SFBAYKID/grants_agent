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
import re
import sqlite3
from dataclasses import dataclass
from typing import cast

from ..sources.base import polite_get

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
PAGE_SIZE = 2_000
MAX_PAGES = 100
_GENERIC = {
    "school", "schools", "district", "public", "unified", "union", "elementary",
    "secondary", "high", "community", "consolidated", "independent", "local",
    "education", "educational", "agency", "sd", "usd", "isd", "lea",
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


def parse_districts(enrollment_payload: dict[str, object],
                    location_payload: dict[str, object]) -> list[NCESDistrict]:
    """Aggregate school membership and merge LEA office cities by NCES ID."""
    cities = {
        str(item.get("LEAID") or "").strip(): str(item.get("CITY") or "").strip()
        for item in _features(location_payload)
        if str(item.get("LEAID") or "").strip()
    }
    aggregates: dict[tuple[str, str, str], int] = {}
    for item in _features(enrollment_payload):
        nces_id = str(item.get("LEAID") or "").strip()
        name = str(item.get("LEA_NAME") or "").strip()
        state = str(item.get("LSTATE") or "").strip().upper()
        try:
            enrollment = int(round(float(
                item.get("ENROLLMENT")
                if item.get("ENROLLMENT") is not None else item.get("MEMBER") or 0)))
        except (TypeError, ValueError):
            continue
        if nces_id and name and state and enrollment >= 0:
            key = (nces_id, name, state)
            aggregates[key] = aggregates.get(key, 0) + enrollment
    districts: list[NCESDistrict] = []
    for (nces_id, name, state), enrollment in aggregates.items():
        districts.append(NCESDistrict(
            nces_id, name, state, cities.get(nces_id, ""), enrollment))
    return districts


def match_district(entity_name: str,
                   districts: list[NCESDistrict]) -> NCESDistrict | None:
    """Return only a unique exact normalized-name match; ambiguity is no match."""
    key = normalize_name(entity_name)
    if not key:
        return None
    matches = [district for district in districts if normalize_name(district.name) == key]
    return matches[0] if len(matches) == 1 else None


def _fetch_all_features(url: str,
                        base_params: dict[str, str]) -> dict[str, object]:
    """Page an ArcGIS query fully and fail closed if pagination does not advance."""
    collected: list[dict[str, object]] = []
    seen_pages: set[str] = set()
    offset = 0
    for _page in range(MAX_PAGES):
        params = {
            **base_params,
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
        }
        payload = cast(dict[str, object], polite_get(url, params=params).json())
        attributes = _features(payload)
        signature = json.dumps(attributes, sort_keys=True, default=str)
        if attributes and signature in seen_pages:
            raise ValueError("NCES pagination repeated a page without advancing")
        if attributes:
            seen_pages.add(signature)
            collected.extend(attributes)
        more = payload.get("exceededTransferLimit") is True or len(attributes) >= PAGE_SIZE
        if not more:
            return {"features": [{"attributes": item} for item in collected]}
        if not attributes:
            raise ValueError("NCES reported more rows but returned an empty page")
        offset += len(attributes)
    raise ValueError(f"NCES pagination exceeded {MAX_PAGES} pages")


def fetch_state(state: str) -> list[NCESDistrict]:
    """Fetch and merge one state's current district membership/location data."""
    state_code = state.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", state_code):
        raise ValueError("NCES enrichment requires a two-letter state")
    enrollment_params = {
        # NCES negative MEMBER values are missing/not-applicable sentinels, not pupils.
        "where": f"LSTATE='{state_code}' AND MEMBER>=0",
        # ArcGIS repeats the first page for this service when a grouped-statistics
        # query uses resultOffset. Page stable school rows and aggregate by LEA here.
        "outFields": "LEAID,LEA_NAME,LSTATE,MEMBER",
        "orderByFields": "OBJECTID",
        "returnGeometry": "false",
        "f": "json",
    }
    location_params = {
        "where": f"STATE='{state_code}'",
        "outFields": "LEAID,NAME,STATE,CITY",
        "orderByFields": "OBJECTID",
        "returnGeometry": "false",
        "f": "json",
    }
    enrollments = _fetch_all_features(SCHOOL_QUERY_URL, enrollment_params)
    locations = _fetch_all_features(LEA_QUERY_URL, location_params)
    return parse_districts(enrollments, locations)


def enrich_state_leads(conn: sqlite3.Connection, state: str,
                       districts: list[NCESDistrict] | None = None) -> EnrichmentSummary:
    """Attach NCES facts to uniquely matching school-like leads in one state."""
    state_code = state.strip().upper()
    reference = districts if districts is not None else fetch_state(state_code)
    rows = list(conn.execute(
        """SELECT id,entity_name FROM leads
           WHERE UPPER(state)=? AND nces_id IS NULL
             AND (LOWER(COALESCE(entity_type,'')) IN
                    ('school','district','school_district','nonpublic_school')
                  OR UPPER(entity_name) LIKE '%SCHOOL%'
                  OR UPPER(entity_name) LIKE '%DISTRICT%'
                  OR UPPER(entity_name) LIKE '% USD'
                  OR UPPER(entity_name) LIKE '% ISD')""",
        (state_code,),
    ))
    matched = 0
    with conn:
        for row in rows:
            district = match_district(str(row["entity_name"]), reference)
            if district is None:
                continue
            conn.execute(
                """UPDATE leads SET nces_id=?,enrollment=?,location_city=?,
                          location_confidence='high' WHERE id=?""",
                (district.nces_id, district.enrollment, district.city or None,
                 int(row["id"])),
            )
            matched += 1
    return EnrichmentSummary(len(rows), matched, len(rows) - matched)
