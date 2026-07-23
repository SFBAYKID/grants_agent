"""Write-free rich-card evidence assembly and bounded candidate review.

This module joins only persisted local evidence. It performs no Slack, Salesforce,
Firecrawl, Anthropic, Persequor, or email call. A separate preparation command may
refresh bounded evidence before invoking this review; shadow mode uses this exact path
on a read-only connection and therefore cannot mutate even SQLite sidecars.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import scoring, territory
from ..enrich.salesforce_activity import RECENT_ACTIVITY_DAYS
from . import card, policy
from .routing import OwnerEvidence, resolve
from .snapshot import SnapshotDraft


@dataclass(frozen=True)
class CandidateReview:
    """One candidate's first rejection cause or complete renderable draft."""

    lead_id: int
    reason: policy.Reason
    draft: SnapshotDraft | None = None


def _rows(conn: sqlite3.Connection, audience: str, limit: int) -> list[sqlite3.Row]:
    """Return bounded unsurfaced Gold projections with their current event/run."""
    candidates = list(
        conn.execute(
            """SELECT l.*, e.id AS event_id, e.observation_id,
                      e.event_type, e.occurred_on, e.date_precision,
                      e.verification_status AS event_verification_status,
                      e.source_url AS event_source_url,
                      r.state AS confirmed_run_state
               FROM leads l
               LEFT JOIN funding_events e ON e.id=l.current_event_id
               LEFT JOIN runs r ON r.id=l.last_confirmed_run_id
               WHERE l.lead_grade='gold' AND COALESCE(l.status,'new')='new'
                 AND l.id NOT IN (SELECT lead_id FROM posts
                                  WHERE lead_id IS NOT NULL AND channel=?)
                 AND l.id NOT IN (SELECT lead_id FROM notification_outbox
                                  WHERE lead_id IS NOT NULL AND audience=?)""",
            (audience, audience),
        )
    )
    candidates.sort(
        key=lambda row: (
            scoring.lead_score(
                str(row["program"] or ""),
                row["amount"],
                str(row["occurred_on"] or ""),
            ),
            -int(row["id"]),
        ),
        reverse=True,
    )
    return candidates[: max(1, min(limit, 500))]


def _latest(
    conn: sqlite3.Connection, table: str, lead_id: int, order_column: str
) -> sqlite3.Row | None:
    """Load the latest append-only evidence row from an allowlisted table shape."""
    allowed = {
        ("contact_evidence", "last_checked_at"),
        ("organization_kind_evidence", "verified_at"),
        ("salesforce_activity_snapshots", "checked_at"),
    }
    if (table, order_column) not in allowed:
        raise ValueError("unsupported evidence table")
    return conn.execute(
        f"SELECT * FROM {table} WHERE lead_id=? "
        f"ORDER BY {order_column} DESC, rowid DESC LIMIT 1",
        (lead_id,),
    ).fetchone()


def _kind(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, str]:
    """Resolve kind from NCES or explicit evidence, never from name heuristics."""
    if str(row["nces_id"] or ""):
        return "school_district", "nces"
    evidence = _latest(
        conn, "organization_kind_evidence", int(row["id"]), "verified_at"
    )
    if evidence is None or str(evidence["status"]) != "verified":
        return "", ""
    return str(evidence["kind"]), str(evidence["provenance"])


def _crm(
    conn: sqlite3.Connection, lead_id: int
) -> tuple[str, str, sqlite3.Row | None, sqlite3.Row | None, frozenset[str]]:
    """Return complete CRM state, timestamp, exact Account/Opportunity and people ids."""
    state = conn.execute(
        "SELECT * FROM salesforce_lookup_state WHERE lead_id=?", (lead_id,)
    ).fetchone()
    if state is None:
        return "", "", None, None, frozenset()
    status = str(state["status"])
    checked_at = str(state["checked_at"])
    if status == "no_match":
        return "complete_no_match", checked_at, None, None, frozenset()
    if status != "found":
        return status, checked_at, None, None, frozenset()
    matches = conn.execute(
        "SELECT * FROM salesforce_matches WHERE lead_id=? AND confidence='high'",
        (lead_id,),
    ).fetchall()
    accounts = [item for item in matches if item["sobject"] == "Account"]
    if len(accounts) != 1:
        return "ambiguous", checked_at, None, None, frozenset()
    account = accounts[0]
    opportunities = [
        item
        for item in matches
        if item["sobject"] == "Opportunity"
        and item["account_id"] == account["record_id"]
        and not bool(item["is_closed"])
    ]
    opportunity = (
        sorted(opportunities, key=lambda item: str(item["record_id"]))[0]
        if opportunities
        else None
    )
    people = frozenset(
        str(item["record_id"])
        for item in matches
        if item["sobject"] in {"Contact", "Lead"}
        and (item["sobject"] == "Lead" or item["account_id"] == account["record_id"])
    )
    return "exact_match", checked_at, account, opportunity, people


def _fresh_activity(
    conn: sqlite3.Connection, lead_id: int, now: datetime
) -> sqlite3.Row | None:
    """Return only a fresh verified call; old/outage results provide no claim/route."""
    row = _latest(conn, "salesforce_activity_snapshots", lead_id, "checked_at")
    if row is None or str(row["status"]) != "verified_call":
        return None
    try:
        checked = datetime.fromisoformat(str(row["checked_at"]).replace("Z", "+00:00"))
        completed = datetime.fromisoformat(
            str(row["completed_at"]).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    if now - checked > timedelta(hours=policy.CRM_FRESH_HOURS):
        return None
    if completed > now or now - completed > timedelta(days=RECENT_ACTIVITY_DAYS):
        return None
    return row


def _candidate(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    audience: str,
    channel_members: frozenset[str],
    now: datetime,
) -> CandidateReview:
    """Assemble and judge one row; build a draft only after every rule passes."""
    lead_id = int(row["id"])
    contact = _latest(conn, "contact_evidence", lead_id, "last_checked_at")
    entity_kind, kind_provenance = _kind(conn, row)
    crm_state, crm_checked, account, opportunity, _people = _crm(conn, lead_id)
    activity = _fresh_activity(conn, lead_id, now)
    award_url = str(row["event_source_url"] or row["detail_url"] or "")
    website = card.safe_url(row["org_website"] or "")
    contact_url = card.safe_url(contact["official_evidence_url"] if contact else "")
    evidence = policy.CandidateEvidence(
        lead_id=lead_id,
        lead_grade=str(row["lead_grade"] or ""),
        event_type=str(row["event_type"] or ""),
        event_verified=str(row["event_verification_status"] or "") == "verified",
        amount=float(row["amount"]) if row["amount"] is not None else None,
        award_date=str(row["occurred_on"] or ""),
        award_date_precision=str(row["date_precision"] or ""),
        spend_window_start=str(row["funds_start"] or ""),
        spend_window_end=str(row["funds_end"] or ""),
        last_confirmed_at=str(row["last_confirmed_at"] or ""),
        last_confirmed_run_complete=str(row["confirmed_run_state"] or "") == "complete",
        entity_kind=entity_kind,
        entity_kind_provenance=kind_provenance,
        state_verified=territory.state_is_verified(row["source"]),
        award_url_safe=bool(card.safe_url(award_url)),
        official_website=website,
        contact_status=str(contact["status"] if contact else ""),
        contact_type=str(contact["contact_type"] if contact else ""),
        contact_email=str(contact["email"] if contact else ""),
        contact_official_domain=str(contact["official_domain"] if contact else ""),
        contact_last_verified_at=str(contact["last_verified_at"] if contact else ""),
        contact_expires_at=str(contact["expires_at"] if contact else ""),
        contact_evidence_url_safe=bool(contact_url),
        program_fit_ok=scoring.PROGRAM_FIT.get(str(row["program"] or "").upper(), 0.0)
        >= 0.9,
        crm_state=crm_state,
        crm_checked_at=crm_checked,
        today=now.date(),
        now_utc=now,
    )
    eligibility = policy.evaluate(evidence)
    if not eligibility.eligible or contact is None:
        return CandidateReview(lead_id, eligibility.reason)

    call_owner = OwnerEvidence(
        str(activity["owner_user_id"] or "") if activity else "",
        str(activity["owner_email"] or "") if activity else "",
    )
    account_owner = OwnerEvidence(
        str(account["owner_id"] or "") if account else "",
        str(account["owner_email"] or "") if account else "",
    )
    opportunity_owner = OwnerEvidence(
        str(opportunity["owner_id"] or "") if opportunity else "",
        str(opportunity["owner_email"] or "") if opportunity else "",
    )
    route = resolve(
        call_owner=call_owner,
        account_owner=account_owner,
        opportunity_owner=opportunity_owner,
        state=str(row["state"] or ""),
        state_source=str(row["source"] or ""),
        channel_members=channel_members,
    )
    sf_display = ""
    if activity is not None:
        completed = datetime.fromisoformat(
            str(activity["completed_at"]).replace("Z", "+00:00")
        )
        sf_display = f"Exact Account match; completed call recorded {(now - completed).days} days ago."
    elif account is not None:
        sf_display = "Exact Account match."
        if opportunity is not None:
            sf_display += " Open Opportunity present."
    draft = SnapshotDraft(
        audience=audience,
        lead_id=lead_id,
        event_id=int(row["event_id"]),
        observation_id=int(row["observation_id"]),
        run_id=int(row["last_confirmed_run_id"]),
        source_item_id=str(row["source_item_id"]),
        canonical_entity_key=str(row["canonical_entity_key"]),
        award_identity=str(row["source_item_id"]),
        tier=eligibility.tier,
        entity_name=str(row["entity_name"]),
        entity_kind=entity_kind,
        entity_kind_provenance=kind_provenance,
        state=str(row["state"] or ""),
        state_provenance=str(row["source"]),
        program=str(row["program"] or ""),
        amount=float(row["amount"]),
        award_date=str(row["occurred_on"]),
        award_date_precision=str(row["date_precision"]),
        spend_window_start=str(row["funds_start"]),
        spend_window_end=str(row["funds_end"]),
        award_url=card.safe_url(award_url),
        official_website=website,
        contact_evidence_id=str(contact["id"]),
        contact_evidence_hash=str(contact["evidence_hash"] or ""),
        contact_name=str(contact["name"] or ""),
        contact_title=str(contact["title"] or ""),
        contact_type=str(contact["contact_type"]),
        contact_email=str(contact["email"]),
        contact_evidence_url=contact_url,
        contact_verified_at=str(contact["last_verified_at"]),
        contact_expires_at=str(contact["expires_at"]),
        sf_lookup_status=crm_state,
        sf_account_id=str(account["record_id"] or "") if account else "",
        sf_open_opp_id=str(opportunity["record_id"] or "") if opportunity else "",
        sf_activity_id=str(activity["activity_id"] or "") if activity else "",
        sf_activity_completed_at=str(activity["completed_at"] or "")
        if activity
        else "",
        sf_activity_owner_user_id=str(activity["owner_user_id"] or "")
        if activity
        else "",
        sf_activity_owner_email=str(activity["owner_email"] or "") if activity else "",
        sf_activity_checked_at=str(activity["checked_at"] or "") if activity else "",
        sf_display_text=sf_display,
        sf_open_link=str(account["link"] or "") if account else "",
        route=route,
        fallback_text="",
        expires_at=str(contact["expires_at"]),
    )
    draft = SnapshotDraft(
        **{**draft.__dict__, "fallback_text": card.fallback_text(draft)}
    )
    return CandidateReview(lead_id, policy.Reason.ELIGIBLE, draft)


def review_candidates(
    conn: sqlite3.Connection,
    audience: str,
    channel_members: frozenset[str],
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> tuple[CandidateReview, ...]:
    """Review a bounded candidate set using persisted evidence only (zero writes)."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return tuple(
        _candidate(conn, row, audience, channel_members, at)
        for row in _rows(conn, audience, limit)
    )
