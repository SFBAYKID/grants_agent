"""Typed read-only evidence for recent completed Salesforce calls.

This boundary never infers a call from ``Subject`` or ``LastActivityDate``. It accepts
only a closed Task explicitly typed ``TaskSubtype='Call'``, bound to the exact Account
and one of its exact Contact/Lead ids, with a completion timestamp and exact Owner
User id/email. Results are persisted locally as append-only snapshots; Salesforce is
never written.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import requests

from .. import roster
from . import salesforce

RECENT_ACTIVITY_DAYS = 30
_SF_ID_RE = re.compile(r"^[A-Za-z0-9]{15,18}$")


class ActivityStatus(str, Enum):
    """Honest result of one completed-call lookup."""

    VERIFIED_CALL = "verified_call"
    NO_RECENT_CALL = "no_recent_call"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ActivityEvidence:
    """One typed lookup result, including exact Salesforce owner identity."""

    status: ActivityStatus
    checked_at: datetime
    activity_id: str = ""
    activity_type: str = ""
    completed_at: datetime | None = None
    account_id: str = ""
    person_id: str = ""
    owner_user_id: str = ""
    owner_name: str = ""
    owner_email: str = ""
    owner_slack_id: str = ""
    record_link: str = ""
    error: str = ""


def _parse_timestamp(value: object) -> datetime | None:
    """Parse one Salesforce timestamp as UTC; date-only values are insufficient."""
    text = str(value or "").strip()
    if not text or "T" not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_id(value: object) -> str:
    """Return a validated Salesforce id or an empty fail-closed marker."""
    text = str(value or "").strip()
    return text if _SF_ID_RE.fullmatch(text) else ""


def lookup_recent_completed_call(
    account_id: str,
    person_ids: frozenset[str],
    *,
    now: datetime | None = None,
) -> ActivityEvidence:
    """Read the newest exact completed call for an Account-bound person.

    Invalid or absent identity inputs are an honest no-result, not an attempted broad
    Salesforce query. Transport/schema failures are ``unavailable``, never no-match.
    """
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    account = _safe_id(account_id)
    people = frozenset(filter(None, (_safe_id(value) for value in person_ids)))
    if not account or not people:
        return ActivityEvidence(ActivityStatus.NO_RECENT_CALL, checked_at)
    query = (
        "SELECT Id,TaskSubtype,Status,IsClosed,CompletedDateTime,AccountId,WhoId,"
        "Owner.Id,Owner.Name,Owner.Email FROM Task "
        f"WHERE AccountId='{account}' AND IsClosed=true AND TaskSubtype='Call' "
        "ORDER BY CompletedDateTime DESC NULLS LAST LIMIT 50"
    )
    try:
        records, instance_url = salesforce.readonly_soql(query)
    except (
        salesforce.SalesforceConfigurationError,
        requests.RequestException,
        KeyError,
        ValueError,
        RuntimeError,
    ) as exc:
        return ActivityEvidence(
            ActivityStatus.UNAVAILABLE,
            checked_at,
            error=f"Salesforce activity lookup failed ({type(exc).__name__})",
        )

    cutoff = checked_at - timedelta(days=RECENT_ACTIVITY_DAYS)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        completed = _parse_timestamp(record.get("CompletedDateTime"))
        if (
            record.get("TaskSubtype") != "Call"
            or not bool(record.get("IsClosed"))
            or _safe_id(record.get("AccountId")) != account
            or _safe_id(record.get("WhoId")) not in people
            or completed is None
            or completed < cutoff
            or completed > checked_at
        ):
            continue
        candidates.append((completed, record))
    if not candidates:
        return ActivityEvidence(ActivityStatus.NO_RECENT_CALL, checked_at)

    completed, record = max(candidates, key=lambda item: item[0])
    owner = record.get("Owner") or {}
    owner_email = str(owner.get("Email") or "").strip().lower()
    activity_id = _safe_id(record.get("Id"))
    owner_id = _safe_id(owner.get("Id"))
    if not activity_id or not owner_id or not owner_email:
        return ActivityEvidence(
            ActivityStatus.UNAVAILABLE,
            checked_at,
            error="Salesforce call evidence omitted exact activity/owner identity",
        )
    return ActivityEvidence(
        status=ActivityStatus.VERIFIED_CALL,
        checked_at=checked_at,
        activity_id=activity_id,
        activity_type="Call",
        completed_at=completed,
        account_id=account,
        person_id=_safe_id(record.get("WhoId")),
        owner_user_id=owner_id,
        owner_name=str(owner.get("Name") or ""),
        owner_email=owner_email,
        owner_slack_id=roster.slack_for_email(owner_email) or "",
        record_link=f"{instance_url}/lightning/r/Task/{activity_id}/view",
    )


def persist(conn: sqlite3.Connection, lead_id: int, evidence: ActivityEvidence) -> str:
    """Append one local activity lookup snapshot and return its opaque id."""
    snapshot_id = uuid.uuid4().hex
    roster_status = (
        "exact"
        if evidence.owner_slack_id
        else "unmapped"
        if evidence.status is ActivityStatus.VERIFIED_CALL
        else "not_applicable"
    )
    with conn:
        conn.execute(
            """INSERT INTO salesforce_activity_snapshots
                 (id,lead_id,status,activity_id,activity_type,completed_at,account_id,
                  person_id,owner_user_id,owner_name,owner_email,owner_slack_id,
                  roster_status,record_link,checked_at,error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                lead_id,
                evidence.status.value,
                evidence.activity_id or None,
                evidence.activity_type or None,
                evidence.completed_at.isoformat() if evidence.completed_at else None,
                evidence.account_id or None,
                evidence.person_id or None,
                evidence.owner_user_id or None,
                evidence.owner_name or None,
                evidence.owner_email or None,
                evidence.owner_slack_id or None,
                roster_status,
                evidence.record_link or None,
                evidence.checked_at.isoformat(),
                evidence.error or None,
            ),
        )
    return snapshot_id
