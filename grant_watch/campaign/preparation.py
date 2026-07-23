"""Write-free rich-card evidence assembly and bounded candidate review.

This module joins only persisted local evidence. It performs no Slack, Salesforce,
Firecrawl, Anthropic, Persequor, or email call. A separate preparation command may
refresh bounded evidence before invoking this review; shadow mode uses this exact path
on a read-only connection and therefore cannot mutate even SQLite sidecars.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from .. import scoring, territory
from ..enrich.salesforce_activity import RECENT_ACTIVITY_DAYS
from . import card, policy
from .routing import OwnerEvidence, resolve
from .snapshot import SnapshotDraft


@dataclass(frozen=True)
class Readiness:
    """Independent PII-free evidence dimensions for shadow diagnostics."""

    contact: bool = False
    salesforce: bool = False
    organization_kind: bool = False
    source_run_link: bool = False
    mapped_route: bool = False
    unassigned_route: bool = False


@dataclass(frozen=True)
class CandidateReview:
    """One candidate's first rejection cause, readiness, or renderable draft."""

    lead_id: int
    reason: policy.Reason
    draft: SnapshotDraft | None = None
    readiness: Readiness = Readiness()


def _rows(conn: sqlite3.Connection, audience: str, limit: int) -> list[sqlite3.Row]:
    """Return bounded unsurfaced Gold projections with their current event/run."""
    candidates = list(
        conn.execute(
            """SELECT l.*, e.id AS event_id, e.observation_id,
                      e.event_type, e.occurred_on, e.date_precision,
                      e.amount AS event_amount,
                      e.verification_status AS event_verification_status,
                      e.source_url AS event_source_url,
                      e.source_locator AS event_source_locator,
                      e.evidence_excerpt AS event_evidence_excerpt,
                      e.evidence_hash AS event_evidence_hash,
                      o.source AS observation_source,
                      o.source_locator AS observation_source_locator,
                      o.payload_hash AS observation_payload_hash,
                      r.state AS confirmed_run_state
               FROM leads l
               LEFT JOIN funding_events e ON e.id=l.current_event_id
               LEFT JOIN source_observations o ON o.id=e.observation_id
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
    """Resolve v1 school-district kind from runtime NCES evidence only."""
    del conn
    if str(row["nces_id"] or ""):
        return "school_district", "nces"
    return "", ""


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
    if len(opportunities) > 1:
        return "ambiguous", checked_at, account, None, frozenset()
    opportunity = opportunities[0] if opportunities else None
    people = frozenset(
        str(item["record_id"])
        for item in matches
        if item["sobject"] in {"Contact", "Lead"}
        and (item["sobject"] == "Lead" or item["account_id"] == account["record_id"])
    )
    return "exact_match", checked_at, account, opportunity, people


def _fresh_activity(
    conn: sqlite3.Connection,
    lead_id: int,
    account_id: str,
    person_ids: frozenset[str],
    now: datetime,
) -> sqlite3.Row | None:
    """Return a fresh call still bound to the current exact CRM identities."""
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
    if checked > now or now - checked > timedelta(hours=policy.CRM_FRESH_HOURS):
        return None
    if completed > now or now - completed > timedelta(days=RECENT_ACTIVITY_DAYS):
        return None
    if (
        str(row["activity_type"] or "") != "Call"
        or str(row["account_id"] or "") != account_id
        or str(row["person_id"] or "") not in person_ids
        or str(row["roster_status"] or "") != "exact"
    ):
        return None
    return row


def _snapshot_expiry(contact_expiry: str, spend_window_end: str) -> str:
    """Use the earliest instant at which any display/action evidence becomes stale."""
    contact = datetime.fromisoformat(contact_expiry.replace("Z", "+00:00"))
    if contact.tzinfo is None:
        contact = contact.replace(tzinfo=timezone.utc)
    window = datetime.combine(
        datetime.fromisoformat(spend_window_end[:10]).date(), time.max, timezone.utc
    )
    return min(contact.astimezone(timezone.utc), window).isoformat()


def _age_days(value: str, now: datetime) -> int | None:
    """Return a non-negative UTC calendar age for readiness reporting."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(
                datetime.fromisoformat(value[:10]).date(), time.min, timezone.utc
            )
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    days = (now.date() - parsed.astimezone(timezone.utc).date()).days
    return days if days >= 0 else None


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
    crm_state, crm_checked, account, opportunity, people = _crm(conn, lead_id)
    account_id = str(account["record_id"] or "") if account else ""
    activity = _fresh_activity(conn, lead_id, account_id, people, now)
    award_url = str(row["event_source_url"] or row["detail_url"] or "")
    website = card.safe_url(row["org_website"] or "")
    website_evidence_url = card.safe_url(row["org_profile_source_url"] or "")
    contact_url = card.safe_url(contact["official_evidence_url"] if contact else "")
    event_locator = str(
        row["event_source_locator"] or row["observation_source_locator"] or ""
    )
    event_hash = str(
        row["event_evidence_hash"] or row["observation_payload_hash"] or ""
    )
    source_name = str(row["observation_source"] or row["source"] or "")
    award_identity = card.award_record_identity(source_name, award_url, event_locator)
    evidence = policy.CandidateEvidence(
        lead_id=lead_id,
        lead_grade=str(row["lead_grade"] or ""),
        event_type=str(row["event_type"] or ""),
        event_verified=str(row["event_verification_status"] or "") == "verified",
        event_evidence_complete=bool(event_locator and event_hash),
        amount=(
            float(row["event_amount"]) if row["event_amount"] is not None else None
        ),
        award_date=str(row["occurred_on"] or ""),
        award_date_precision=str(row["date_precision"] or ""),
        spend_window_start=str(row["funds_start"] or ""),
        spend_window_end=str(row["funds_end"] or ""),
        last_confirmed_at=str(row["last_confirmed_at"] or ""),
        last_confirmed_run_complete=str(row["confirmed_run_state"] or "") == "complete",
        entity_kind=entity_kind,
        entity_kind_provenance=kind_provenance,
        nces_id=str(row["nces_id"] or ""),
        state_verified=territory.state_is_verified(row["source"]),
        award_url_safe=bool(award_identity),
        official_website=website,
        org_profile_found=str(row["org_profile_status"] or "") == "found",
        org_profile_evidence_url=website_evidence_url,
        nces_website="",  # NCES-published official site not yet captured at runtime
        contact_status=str(contact["status"] if contact else ""),
        contact_type=str(contact["contact_type"] if contact else ""),
        contact_email=str(contact["email"] if contact else ""),
        contact_evidence_url=contact_url,
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
    research = eligibility.card_mode is policy.CardMode.RESEARCH_NEEDED
    # A research-needed (ambiguous CRM) card must assert no relationship and route by
    # TERRITORY only — never to a Salesforce owner. Drop every exact CRM binding so an
    # ambiguous single-account/multi-opportunity result cannot leak an owner mention.
    if research:
        account = opportunity = activity = None
        people = frozenset()
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
    confirmed_age = _age_days(evidence.last_confirmed_at, now)
    contact_age = _age_days(evidence.contact_last_verified_at, now)
    email_domain = (
        evidence.contact_email.rsplit("@", 1)[-1].lower()
        if "@" in evidence.contact_email
        else ""
    )
    try:
        contact_expiry = datetime.fromisoformat(
            evidence.contact_expires_at.replace("Z", "+00:00")
        )
    except ValueError:
        contact_expiry = now
    if contact_expiry.tzinfo is None:
        contact_expiry = contact_expiry.replace(tzinfo=timezone.utc)
    try:
        crm_time = datetime.fromisoformat(
            evidence.crm_checked_at.replace("Z", "+00:00")
        )
    except ValueError:
        crm_time = now - timedelta(days=999)
    if crm_time.tzinfo is None:
        crm_time = crm_time.replace(tzinfo=timezone.utc)
    readiness = Readiness(
        contact=(
            evidence.contact_status == "verified"
            and evidence.contact_type in policy.CONTACT_TYPES
            and contact_age is not None
            and contact_age <= policy.CONTACT_FRESH_DAYS
            and contact_expiry > now
            and evidence.contact_evidence_url_safe
            and email_domain not in policy.PERSONAL_EMAIL_DOMAINS
            and policy.contact_binding(
                evidence.contact_email,
                evidence.official_website,
                contact_evidence_url=evidence.contact_evidence_url,
                nces_id=evidence.nces_id,
            )
            is not policy.ContactBinding.NONE
        ),
        salesforce=(
            evidence.crm_state in policy.SAFE_CRM_STATES
            and now - timedelta(hours=policy.CRM_FRESH_HOURS) <= crm_time <= now
        ),
        organization_kind=(
            evidence.entity_kind in policy.QUALIFYING_ENTITY_KINDS
            and evidence.entity_kind_provenance in policy.VALID_KIND_PROVENANCE
        ),
        source_run_link=(
            evidence.last_confirmed_run_complete
            and confirmed_age is not None
            and confirmed_age <= policy.OBSERVATION_FRESH_DAYS
            and evidence.award_url_safe
            and policy.website_provenance(
                evidence.official_website,
                org_profile_found=evidence.org_profile_found,
                org_profile_evidence_url=evidence.org_profile_evidence_url,
                contact_status=evidence.contact_status,
                contact_evidence_url=evidence.contact_evidence_url,
                nces_website=evidence.nces_website,
            )
            is not policy.WebsiteProvenance.NONE
        ),
        mapped_route=bool(route.slack_user_id),
        unassigned_route=not bool(route.slack_user_id),
    )
    if not eligibility.eligible or contact is None:
        return CandidateReview(lead_id, eligibility.reason, readiness=readiness)
    if research:
        sf_display = "Possible Salesforce matches—review before outreach."
    else:
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
        award_identity=award_identity,
        event_type=str(row["event_type"]),
        event_verification_status=str(row["event_verification_status"]),
        event_evidence_excerpt=str(row["event_evidence_excerpt"] or ""),
        event_evidence_hash=event_hash,
        event_source_locator=event_locator,
        tier=eligibility.tier,
        card_mode=eligibility.card_mode.value,
        entity_name=str(row["entity_name"]),
        entity_kind=entity_kind,
        entity_kind_provenance=kind_provenance,
        state=str(row["state"] or ""),
        state_provenance=source_name,
        program=str(row["program"] or ""),
        amount=float(row["event_amount"]),
        award_date=str(row["occurred_on"]),
        award_date_precision=str(row["date_precision"]),
        spend_window_start=str(row["funds_start"]),
        spend_window_end=str(row["funds_end"]),
        award_url=card.safe_url(award_url),
        official_website=website,
        official_website_evidence_url=website_evidence_url,
        official_website_provenance=eligibility.website_provenance.value,
        contact_domain_binding=eligibility.contact_binding.value,
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
        expires_at=_snapshot_expiry(str(contact["expires_at"]), str(row["funds_end"])),
    )
    draft = SnapshotDraft(
        **{**draft.__dict__, "fallback_text": card.fallback_text(draft)}
    )
    return CandidateReview(lead_id, policy.Reason.ELIGIBLE, draft, readiness)


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


def candidate_lead_ids(
    conn: sqlite3.Connection, audience: str, limit: int
) -> tuple[int, ...]:
    """Expose the bounded quality-ordered queue to the preparation worker."""
    return tuple(int(row["id"]) for row in _rows(conn, audience, limit))


def exact_crm_bindings(
    conn: sqlite3.Connection, lead_id: int
) -> tuple[str, frozenset[str]]:
    """Expose only an exact Account id and its bound Contact/Lead ids for activity."""
    state, _checked, account, _opportunity, people = _crm(conn, lead_id)
    if state != "exact_match" or account is None:
        return "", frozenset()
    return str(account["record_id"]), people
