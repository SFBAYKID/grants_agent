"""Freeze and load immutable rich award-card evidence.

Snapshot creation occurs only at the delivery reservation boundary. Shadow/report
paths construct ``SnapshotDraft`` values but never call ``freeze``. Every later thread,
button, feedback, and Persequor action must load this row instead of re-reading the
mutable lead projection.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .policy import POLICY_VERSION
from .routing import Route, RoutingReason


@dataclass(frozen=True)
class SnapshotDraft:
    """All evidence/rendering inputs ready to freeze immediately before delivery."""

    audience: str
    lead_id: int
    event_id: int
    observation_id: int
    run_id: int
    source_item_id: str
    canonical_entity_key: str
    award_identity: str
    event_type: str
    event_verification_status: str
    event_evidence_excerpt: str
    event_evidence_hash: str
    event_source_locator: str
    tier: str
    entity_name: str
    entity_kind: str
    entity_kind_provenance: str
    state: str
    state_provenance: str
    program: str
    amount: float
    award_date: str
    award_date_precision: str
    spend_window_start: str
    spend_window_end: str
    award_url: str
    official_website: str
    official_website_evidence_url: str
    contact_evidence_id: str
    contact_evidence_hash: str
    contact_name: str
    contact_title: str
    contact_type: str
    contact_email: str
    contact_evidence_url: str
    contact_verified_at: str
    contact_expires_at: str
    sf_lookup_status: str
    sf_account_id: str
    sf_open_opp_id: str
    sf_activity_id: str
    sf_activity_completed_at: str
    sf_activity_owner_user_id: str
    sf_activity_owner_email: str
    sf_activity_checked_at: str
    sf_display_text: str
    sf_open_link: str
    route: Route
    fallback_text: str
    expires_at: str


@dataclass(frozen=True)
class FrozenSnapshot:
    """Stored immutable snapshot plus its opaque id and provenance."""

    id: str
    policy_version: int
    created_at: str
    draft: SnapshotDraft


def award_dedup_key(draft: SnapshotDraft) -> str:
    """Hash one source-qualified award identity, independent of evidence versions."""
    stable = "|".join(
        (
            draft.canonical_entity_key.strip().lower(),
            draft.program.strip().lower(),
            draft.state_provenance.strip().lower(),
            draft.award_identity.strip().lower(),
        )
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _snapshot_key(draft: SnapshotDraft) -> str:
    """Identify one exact immutable evidence version of a stable award."""
    fingerprint = hashlib.sha256(_render_inputs(draft).encode("utf-8")).hexdigest()
    return hashlib.sha256(
        f"{award_dedup_key(draft)}|{fingerprint}".encode("utf-8")
    ).hexdigest()


def _render_inputs(draft: SnapshotDraft) -> str:
    """Serialize deterministic rendering inputs without the nested Route object."""
    body = asdict(draft)
    body["route"] = {
        "reason": draft.route.reason.value,
        "slack_user_id": draft.route.slack_user_id,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def freeze(
    conn: sqlite3.Connection,
    draft: SnapshotDraft,
    *,
    now: datetime | None = None,
) -> tuple[FrozenSnapshot, bool]:
    """Insert one immutable evidence version; never update frozen facts.

    The exact same evidence reuses its row after a pre-reservation crash. Changed
    evidence creates a new preparation snapshot; the outbox separately enforces the
    stable award/audience delivery key.
    """
    created_at = (
        (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    )
    snapshot_id = uuid.uuid4().hex
    key = _snapshot_key(draft)
    stable_key = award_dedup_key(draft)
    render_inputs = _render_inputs(draft)
    with conn:
        inserted = conn.execute(
            """INSERT OR IGNORE INTO rich_card_snapshots
                 (id,policy_version,audience,dedup_key,lead_id,event_id,observation_id,
                  run_id,source_item_id,tier,entity_name,entity_kind,
                  entity_kind_provenance,state,state_provenance,program,amount,
                  award_date,award_date_precision,spend_window_start,spend_window_end,
                  award_url,official_website,contact_evidence_id,contact_evidence_hash,contact_name,
                  contact_title,contact_type,contact_email,contact_evidence_url,
                  contact_verified_at,contact_expires_at,sf_lookup_status,sf_account_id,
                  sf_open_opp_id,sf_activity_id,sf_activity_completed_at,
                  sf_activity_owner_user_id,sf_activity_owner_email,
                  sf_activity_checked_at,sf_display_text,sf_open_link,routing_reason,
                  slack_user_id,fallback_text,render_inputs_json,created_at,expires_at,
                  state_updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                POLICY_VERSION,
                draft.audience,
                key,
                draft.lead_id,
                draft.event_id,
                draft.observation_id,
                draft.run_id,
                draft.source_item_id,
                draft.tier,
                draft.entity_name,
                draft.entity_kind,
                draft.entity_kind_provenance,
                draft.state or None,
                draft.state_provenance or None,
                draft.program or None,
                draft.amount,
                draft.award_date,
                draft.award_date_precision,
                draft.spend_window_start,
                draft.spend_window_end,
                draft.award_url,
                draft.official_website,
                draft.contact_evidence_id,
                draft.contact_evidence_hash,
                draft.contact_name or None,
                draft.contact_title or None,
                draft.contact_type,
                draft.contact_email,
                draft.contact_evidence_url,
                draft.contact_verified_at,
                draft.contact_expires_at,
                draft.sf_lookup_status,
                draft.sf_account_id or None,
                draft.sf_open_opp_id or None,
                draft.sf_activity_id or None,
                draft.sf_activity_completed_at or None,
                draft.sf_activity_owner_user_id or None,
                draft.sf_activity_owner_email or None,
                draft.sf_activity_checked_at or None,
                draft.sf_display_text or None,
                draft.sf_open_link or None,
                draft.route.reason.value,
                draft.route.slack_user_id or None,
                draft.fallback_text,
                render_inputs,
                created_at,
                draft.expires_at or None,
                created_at,
            ),
        )
    row = conn.execute(
        "SELECT * FROM rich_card_snapshots WHERE dedup_key=? AND audience=?",
        (key, draft.audience),
    ).fetchone()
    assert row is not None
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO rich_card_snapshot_truth
                 (snapshot_id,award_dedup_key,source_name,event_type,event_amount,
                  event_verification_status,event_evidence_excerpt,event_evidence_hash,
                  event_source_locator,official_website_evidence_url)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"],
                stable_key,
                draft.state_provenance,
                draft.event_type,
                draft.amount,
                draft.event_verification_status,
                draft.event_evidence_excerpt or None,
                draft.event_evidence_hash,
                draft.event_source_locator,
                draft.official_website_evidence_url,
            ),
        )
    return _from_row(row), inserted.rowcount == 1


def _from_row(row: sqlite3.Row) -> FrozenSnapshot:
    """Rehydrate the exact original draft from immutable render inputs."""
    body: dict[str, Any] = json.loads(str(row["render_inputs_json"]))
    route_body = body.pop("route")
    body["route"] = Route(
        reason=RoutingReason(route_body["reason"]),
        slack_user_id=route_body["slack_user_id"],
    )
    return FrozenSnapshot(
        id=str(row["id"]),
        policy_version=int(row["policy_version"]),
        created_at=str(row["created_at"]),
        draft=SnapshotDraft(**body),
    )


def load(conn: sqlite3.Connection, snapshot_id: str) -> FrozenSnapshot | None:
    """Load one immutable snapshot by opaque id."""
    row = conn.execute(
        "SELECT * FROM rich_card_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    return _from_row(row) if row is not None else None


def lead_context(snapshot: FrozenSnapshot) -> dict[str, object]:
    """Expose only frozen facts in the legacy conversation row shape."""
    draft = snapshot.draft
    event_date = (
        draft.award_date[:7]
        if draft.award_date_precision == "month"
        else draft.award_date
    )
    return {
        "id": draft.lead_id,
        "source": draft.state_provenance,
        "entity_name": draft.entity_name,
        "entity_type": draft.entity_kind,
        "state": draft.state,
        "program": draft.program,
        "amount": draft.amount,
        "funds_start": draft.spend_window_start,
        "funds_end": draft.spend_window_end,
        "detail_url": draft.award_url,
        "status": "snapshotted",
        "lead_grade": "gold",
        "current_event_id": draft.event_id,
        "current_event_type": draft.event_type,
        "current_event_occurred_on": event_date,
        "current_event_date_precision": draft.award_date_precision,
        "current_event_verification_status": draft.event_verification_status,
        "current_event_evidence_excerpt": draft.event_evidence_excerpt,
        "current_event_source_url": draft.award_url,
        "current_event_source_locator": draft.event_source_locator,
        "salesforce_status": draft.sf_lookup_status,
        "salesforce_opportunity_link": (
            draft.sf_open_link if draft.sf_open_opp_id else ""
        ),
        "salesforce_account_link": draft.sf_open_link,
        "snapshot_contact": (
            f"{draft.contact_name or 'Official general mailbox'}"
            f"{f' — {draft.contact_title}' if draft.contact_title else ''}: "
            f"{draft.contact_email}; verified {draft.contact_verified_at}; "
            f"evidence {draft.contact_evidence_url}"
        ),
        "snapshot_salesforce": draft.sf_display_text or "Complete no-match.",
        "snapshot_routing": (
            f"<@{draft.route.slack_user_id}> via {draft.route.reason.value}"
            if draft.route.slack_user_id
            else "unassigned; no verified owner mapped"
        ),
        "snapshot_official_website": draft.official_website,
    }
