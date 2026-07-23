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
    contact_evidence_id: str
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


def dedup_key(draft: SnapshotDraft) -> str:
    """Hash stable award identity without policy version or event surrogate id."""
    stable = "|".join(
        (
            draft.canonical_entity_key.strip().lower(),
            draft.program.strip().lower(),
            draft.award_identity.strip().lower(),
        )
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


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
    """Insert once by stable award/audience identity; never update frozen facts.

    Returns the original row when the same award was already frozen. Callers use the
    boolean to distinguish a new delivery reservation from an existing snapshot.
    """
    created_at = (
        (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    )
    snapshot_id = uuid.uuid4().hex
    key = dedup_key(draft)
    render_inputs = _render_inputs(draft)
    with conn:
        inserted = conn.execute(
            """INSERT OR IGNORE INTO rich_card_snapshots
                 (id,policy_version,audience,dedup_key,lead_id,event_id,observation_id,
                  run_id,source_item_id,tier,entity_name,entity_kind,
                  entity_kind_provenance,state,state_provenance,program,amount,
                  award_date,award_date_precision,spend_window_start,spend_window_end,
                  award_url,official_website,contact_evidence_id,contact_name,
                  contact_title,contact_type,contact_email,contact_evidence_url,
                  contact_verified_at,contact_expires_at,sf_lookup_status,sf_account_id,
                  sf_open_opp_id,sf_activity_id,sf_activity_completed_at,
                  sf_activity_owner_user_id,sf_activity_owner_email,
                  sf_activity_checked_at,sf_display_text,sf_open_link,routing_reason,
                  slack_user_id,fallback_text,render_inputs_json,created_at,expires_at,
                  state_updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
