"""Snapshot-bound rich-card feedback and Persequor draft-request actions.

All authorization/context checks happen before durable state. Draft requests persist
before the external call and are unique per snapshot, collapsing Slack retries and
double-clicks. Contact freshness/removal is rechecked; no enrichment runs on click.
The payload uses the existing exact ``outreach-request.v1`` key set unchanged.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .. import db, persequor_client, roster
from .snapshot import FrozenSnapshot, load

Submitter = Callable[
    [sqlite3.Connection, int, persequor_client.OutreachBrief], tuple[str, str]
]


@dataclass(frozen=True)
class ActionResult:
    """Truthful user-facing result of an idempotent card action."""

    state: str
    message: str


def _authorized_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    requester_is_member: bool,
) -> FrozenSnapshot:
    """Validate workspace/channel/thread/requester and return the posted snapshot."""
    expected_workspace = os.environ.get("SLACK_WORKSPACE_ID", "").strip()
    if not workspace or (expected_workspace and workspace != expected_workspace):
        raise PermissionError("workspace could not be verified")
    if (
        not requester
        or not requester_is_member
        or roster.email_for_slack(requester) is None
    ):
        raise PermissionError("requester is not an approved active channel member")
    snapshot = load(conn, snapshot_id)
    if snapshot is None:
        raise ValueError("card snapshot was not found")
    post = conn.execute(
        "SELECT * FROM posts WHERE snapshot_id=? AND channel=? AND ts=?",
        (snapshot_id, channel, thread_ts),
    ).fetchone()
    if post is None or snapshot.draft.audience != channel:
        raise PermissionError("card channel/thread did not match the frozen snapshot")
    return snapshot


def _fresh_contact(
    conn: sqlite3.Connection, snapshot: FrozenSnapshot, now: datetime
) -> bool:
    """Recheck expiry/removal while keeping every submitted fact snapshot-bound."""
    draft = snapshot.draft
    try:
        expires = datetime.fromisoformat(
            draft.contact_expires_at.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    current = conn.execute(
        "SELECT status,email,evidence_hash FROM contact_evidence WHERE id=?",
        (draft.contact_evidence_id,),
    ).fetchone()
    return bool(
        expires > now
        and current is not None
        and current["status"] == "verified"
        and current["email"] == draft.contact_email
        and current["evidence_hash"] == draft.contact_evidence_hash
    )


def _brief(
    snapshot: FrozenSnapshot,
    requester: str,
    channel: str,
    thread_ts: str,
) -> persequor_client.OutreachBrief:
    """Map frozen evidence into the unchanged outreach-request.v1 contract."""
    draft = snapshot.draft
    send_as = roster.email_for_slack(requester)
    assert send_as is not None
    test_email = os.environ.get("OUTREACH_TEST_EMAIL", "").strip()
    request_id = (
        "grant-rich-"
        + hashlib.sha256(f"{snapshot.id}|{requester}".encode()).hexdigest()[:24]
    )
    notes = None
    contact_email = draft.contact_email
    if test_email:
        notes = (
            f"[TEST MODE — send to {test_email} only; real verified contact: "
            f"{draft.contact_name or 'official general mailbox'} "
            f"({draft.contact_title or draft.contact_type}) <{draft.contact_email}>]"
        )
        contact_email = test_email
    entity_types = {
        "school": "school",
        "school_district": "school_district",
        "city": "city",
    }
    return {
        "schema": "outreach-request.v1",
        "request_id": request_id,
        "entity": draft.entity_name,
        "entity_type": entity_types[draft.entity_kind],
        "state": draft.state,
        "program": draft.program,
        "amount_usd": int(round(draft.amount)),
        "window_start": draft.spend_window_start,
        "window_end": draft.spend_window_end,
        "source_url": draft.award_url,
        "requested_by_slack": requester,
        "send_as": send_as,
        "contact_name": draft.contact_name or None,
        "contact_email": contact_email,
        "contact_title": draft.contact_title or None,
        "angle": "verified funding award with a currently open spend window",
        "rep_notes": notes,
        "expires_at": draft.expires_at or None,
        "slack_channel": channel,
        "slack_thread_ts": thread_ts,
    }


def request_draft(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    requester_is_member: bool,
    nonce: str,
    submitter: Submitter = persequor_client.submit_brief,
    now: datetime | None = None,
) -> ActionResult:
    """Persist one requester-bound draft action, then submit the frozen brief once."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    snapshot = _authorized_snapshot(
        conn,
        snapshot_id,
        workspace=workspace,
        channel=channel,
        thread_ts=thread_ts,
        requester=requester,
        requester_is_member=requester_is_member,
    )
    if not _fresh_contact(conn, snapshot, at):
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO rich_card_actions
                     (id,snapshot_id,action,nonce,requester_slack,state,detail,
                      created_at,updated_at)
                   VALUES (?,?, 'draft',?,?, 'blocked_expired',?,?,?)""",
                (
                    uuid.uuid4().hex,
                    snapshot_id,
                    nonce,
                    requester,
                    "contact evidence expired or was removed",
                    at.isoformat(),
                    at.isoformat(),
                ),
            )
        return ActionResult(
            "blocked_expired",
            "The verified contact expired or was removed; no draft was requested.",
        )
    existing = conn.execute(
        "SELECT * FROM rich_card_actions WHERE snapshot_id=? AND action='draft'",
        (snapshot_id,),
    ).fetchone()
    if existing is not None:
        if existing["requester_slack"] != requester:
            return ActionResult("rejected", "Another rep already requested this draft.")
        return ActionResult(
            str(existing["state"]), "This draft request was already handled."
        )
    action_id = uuid.uuid4().hex
    with conn:
        conn.execute(
            """INSERT INTO rich_card_actions
                 (id,snapshot_id,action,nonce,requester_slack,state,created_at,updated_at)
               VALUES (?,?, 'draft',?,?, 'requested',?,?)""",
            (action_id, snapshot_id, nonce, requester, at.isoformat(), at.isoformat()),
        )
    post = conn.execute(
        "SELECT id FROM posts WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    db.record_outcome(
        conn,
        snapshot.draft.lead_id,
        int(post["id"]) if post else None,
        requester,
        "persequor_draft_requested",
        f"rich-action:{action_id}:requested",
    )
    state, message = submitter(
        conn,
        snapshot.draft.lead_id,
        _brief(snapshot, requester, channel, thread_ts),
    )
    action_state = (
        "accepted"
        if state == "submitted"
        else "rejected"
        if state == "rejected"
        else "requested"
    )
    with conn:
        conn.execute(
            "UPDATE rich_card_actions SET state=?,detail=?,updated_at=? WHERE id=?",
            (action_state, message, datetime.now(timezone.utc).isoformat(), action_id),
        )
    outcome_kind = {
        "submitted": "persequor_intake_accepted",
        "rejected": "persequor_intake_rejected",
    }.get(state, "persequor_intake_unavailable")
    db.record_outcome(
        conn,
        snapshot.draft.lead_id,
        int(post["id"]) if post else None,
        requester,
        outcome_kind,
        f"rich-action:{action_id}:{state}",
    )
    return ActionResult(action_state, message)


def mark_not_relevant(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    requester_is_member: bool,
    nonce: str,
) -> ActionResult:
    """Persist deduplicated feedback and suppress the lead in legacy/new selectors."""
    snapshot = _authorized_snapshot(
        conn,
        snapshot_id,
        workspace=workspace,
        channel=channel,
        thread_ts=thread_ts,
        requester=requester,
        requester_is_member=requester_is_member,
    )
    now = datetime.now(timezone.utc).isoformat()
    action_id = uuid.uuid4().hex
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO rich_card_actions
                 (id,snapshot_id,action,nonce,requester_slack,state,created_at,updated_at)
               VALUES (?,?, 'not_relevant',?,?, 'accepted',?,?)""",
            (action_id, snapshot_id, nonce, requester, now, now),
        )
        if cur.rowcount == 1:
            conn.execute(
                "UPDATE leads SET status='not_relevant',status_note=? WHERE id=?",
                (
                    f"rich card marked not relevant by <@{requester}>",
                    snapshot.draft.lead_id,
                ),
            )
    if cur.rowcount == 0:
        return ActionResult("accepted", "This card was already marked not relevant.")
    post = conn.execute(
        "SELECT id FROM posts WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    db.record_outcome(
        conn,
        snapshot.draft.lead_id,
        int(post["id"]) if post else None,
        requester,
        "not_relevant",
        f"rich-action:{action_id}:not-relevant",
    )
    return ActionResult("accepted", "Marked not relevant. No outreach was requested.")
