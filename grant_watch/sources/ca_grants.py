"""California Grants Portal no-key opportunities and post-award recipient data.

Why: the California State Library publishes an official daily CSV plus fiscal-year
award CSVs through data.ca.gov. Only physical-security records pass this source's
conservative filter; cybersecurity and generic safety programs are excluded. Award
publication dates are retained as provenance but never represented as award dates.

Verification: verified live 2026-07-14 against the official CKAN metadata and CSVs.
The current award CSV contains named 2024-25 CSNSGP recipients and amounts. Live
polling remains separate from offline parser tests.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from typing import cast

from ..models import (
    DatePrecision,
    FundingEventType,
    RawItem,
    VerificationStatus,
)
from .base import polite_get

CKAN_PACKAGE_API = "https://data.ca.gov/api/3/action/package_show"
OPPORTUNITY_PACKAGE = "california-grants-portal"
AWARD_PACKAGES = (
    "california-grants-portal-grant-awards-2022-2023",
    "california-grants-portal-grant-awards-2023-2024",
    "california-grants-portal-grant-awards-2024-2025",
)
BACKFILL_DAYS = 90

_PHYSICAL_RE = re.compile(
    r"nonprofit security grant|target hardening|physical security|security camera"
    r"|video surveillance|access control system|door hardening|panic alarm"
    r"|visitor management|perimeter security|school security technology",
    re.IGNORECASE,
)
_CYBER_RE = re.compile(
    r"cybersecurity|multi-factor|single sign-on|identity provider|data encryption"
    r"|penetration testing|SIEM|information security",
    re.IGNORECASE,
)
_TARGET_ENTITY_RE = re.compile(
    r"school|district|academy|college|university|city of|county of|\bcity\b|\bcounty\b",
    re.IGNORECASE,
)


def _date_iso(raw: str) -> str:
    """Normalize a portal timestamp/date without guessing malformed values."""
    value = raw.strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _money(raw: str) -> float | None:
    """Parse one portal currency cell; narrative/range values remain unknown."""
    value = raw.strip()
    if not value or re.search(r"denied|cancel|dependent|dependant", value, re.I):
        return None
    normalized = re.sub(r"[$,\s]", "", value)
    try:
        return float(normalized)
    except ValueError:
        return None


def _physical_security(text: str) -> bool:
    """Return true for catalog-relevant physical security, excluding cyber matches."""
    return bool(_PHYSICAL_RE.search(text)) and not bool(_CYBER_RE.search(text))


def _is_backfill(published: str, today: date) -> bool:
    """Suppress initial alert waves for records published more than 90 days ago."""
    parsed = _date_iso(published)
    return not parsed or parsed < (today - timedelta(days=BACKFILL_DAYS)).isoformat()


def parse_opportunities(csv_text: str, today: date | None = None) -> list[RawItem]:
    """Parse active, physically relevant state grant opportunities from official CSV."""
    today = today or date.today()
    out: list[RawItem] = []
    normalized = csv_text.removeprefix("\ufeff").removeprefix("ï»¿")
    for row in csv.DictReader(io.StringIO(normalized)):
        if (row.get("Status") or "").strip().lower() != "active":
            continue
        evidence = " ".join(
            row.get(field) or "" for field in ("Title", "Purpose", "Description")
        )
        if not _physical_security(evidence):
            continue
        item_id = (row.get("PortalID") or row.get("GrantID") or "").strip()
        if not item_id:
            continue
        opened = _date_iso(row.get("OpenDate") or "")
        deadline = _date_iso(row.get("ApplicationDeadline") or "")
        if deadline and deadline < today.isoformat():
            continue
        out.append(
            RawItem(
                source="ca-grants-portal",
                item_id=item_id,
                title=(row.get("Title") or "")[:300],
                entity=(row.get("AgencyDept") or "California state agency").strip(),
                state="CA",
                program=(row.get("Title") or "California grant")[:120],
                amount=_money(row.get("EstAvailFunds") or ""),
                start=opened,
                end=deadline,
                url=(row.get("GrantURL") or row.get("AgencyURL") or "").strip(),
                raw={
                    key: row.get(key)
                    for key in (
                        "PortalID",
                        "Status",
                        "LastUpdated",
                        "ApplicantType",
                        "Geography",
                        "EstAvailFunds",
                        "EstAmounts",
                        "AwardStats",
                    )
                },
                event_type=FundingEventType.APPLICATION_WINDOW_OPENED,
                event_date=opened,
                date_precision=DatePrecision.DAY if opened else DatePrecision.UNKNOWN,
                eligible_scope=(
                    row.get("ApplicantTypeNotes") or row.get("ApplicantType") or ""
                )[:500],
                application_portal=(row.get("ElecSubmission") or "")[:500],
                source_locator=f"PortalID {item_id}",
                evidence_excerpt=evidence[:500],
                verification_status=VerificationStatus.VERIFIED,
                backfill=_is_backfill(row.get("LastUpdated") or opened, today),
            )
        )
    return out


def parse_awards(
    csv_text: str, fiscal_year: str, source_url: str, today: date | None = None
) -> list[RawItem]:
    """Parse approved physical-security awards without treating publish date as award date."""
    today = today or date.today()
    out: list[RawItem] = []
    normalized = csv_text.removeprefix("\ufeff").removeprefix("ï»¿")
    for row in csv.DictReader(io.StringIO(normalized)):
        notes = " ".join(
            (row.get("AwardAmountNotes") or "", row.get("AwardCancellingNotes") or "")
        )
        if re.search(r"denied|cancel", notes, re.I):
            continue
        evidence = " ".join(
            row.get(field) or "" for field in ("ProjectTitle", "ProjectAbstract")
        )
        entity = (row.get("RecipientName") or "").strip()
        if not entity or not _physical_security(evidence):
            continue
        if (
            "nonprofit security grant" not in evidence.lower()
            and not _TARGET_ENTITY_RE.search(entity)
        ):
            continue
        item_id = (row.get("PortalID") or "").strip()
        if not item_id:
            continue
        published = row.get("PublishDate") or row.get("LastUpdated") or ""
        out.append(
            RawItem(
                source=f"ca-grants-award:{fiscal_year}",
                item_id=item_id,
                title=(row.get("ProjectTitle") or "California grant award")[:300],
                entity=entity,
                state="CA",
                program=(row.get("ProjectTitle") or "California state grant")[:120],
                amount=_money(row.get("TotalAwardAmount") or ""),
                start=_date_iso(row.get("ProjectStartDate") or ""),
                end=_date_iso(row.get("ProjectEndDate") or ""),
                url=source_url,
                raw={
                    key: row.get(key)
                    for key in (
                        "PortalID",
                        "GrantID",
                        "PublishDate",
                        "LastUpdated",
                        "FiscalYear",
                        "RecipientType",
                        "ProjectStatus",
                        "CountiesServed",
                    )
                },
                event_type=FundingEventType.AWARD_OBLIGATED,
                # PublishDate is portal-publication provenance, not an award action date.
                event_date="",
                date_precision=DatePrecision.UNKNOWN,
                funded_scope=(row.get("ProjectAbstract") or "")[:500],
                source_locator=f"Portal award {item_id}",
                evidence_excerpt=evidence[:500],
                verification_status=VerificationStatus.VERIFIED,
                backfill=_is_backfill(published, today),
            )
        )
    return out


def _csv_resource_url(package_id: str) -> str:
    """Resolve the current official CSV resource URL from California's CKAN API."""
    payload = cast(
        dict[str, object],
        polite_get(CKAN_PACKAGE_API, params={"id": package_id}).json(),
    )
    if payload.get("success") is not True or not isinstance(
        payload.get("result"), dict
    ):
        raise ValueError(f"California package metadata unavailable for {package_id}")
    result = cast(dict[str, object], payload["result"])
    resources = result.get("resources")
    if not isinstance(resources, list):
        raise ValueError(f"California package {package_id} has no resources")
    for candidate in resources:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("format") or "").upper() == "CSV":
            url = str(candidate.get("url") or "").strip()
            if url.startswith("https://data.ca.gov/"):
                return url
    raise ValueError(f"California package {package_id} has no official CSV")


def poll() -> list[RawItem]:
    """Fetch the daily opportunity file plus every currently published award year."""
    opportunity_url = _csv_resource_url(OPPORTUNITY_PACKAGE)
    out = parse_opportunities(polite_get(opportunity_url).content.decode("utf-8-sig"))
    for package_id in AWARD_PACKAGES:
        resource_url = _csv_resource_url(package_id)
        fiscal_year = package_id.rsplit("-awards-", 1)[-1]
        out.extend(
            parse_awards(
                polite_get(resource_url).content.decode("utf-8-sig"),
                fiscal_year,
                resource_url,
            )
        )
    return out
