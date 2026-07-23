"""Snapshot-first, reservation-before-Slack rich-card delivery.

The feature flag dispatch lives in ``cli.py``; this module assumes it was selected.
Dry-run performs only local reads and never freezes, reserves, posts, or mutates. An
ambiguous Slack outcome is finalized ``unknown`` and never blind-retried.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError

from .. import db
from . import card, pacing
from .preparation import CandidateReview, review_candidates
from .snapshot import freeze


class SlackPoster(Protocol):
    """Narrow Slack surface used by rich delivery and fixture fakes."""

    def chat_postMessage(self, **kwargs: object) -> dict[str, Any]:
        """Post one message and return a mapping containing ``ts``."""


def channel_members(client: Any, channel: str) -> frozenset[str]:
    """Read every configured-channel member; any Slack failure returns no mappings."""
    members: set[str] = set()
    cursor = ""
    try:
        while True:
            response = client.conversations_members(
                channel=channel, limit=200, cursor=cursor or None
            )
            members.update(str(value) for value in response.get("members", []))
            cursor = str(
                (response.get("response_metadata") or {}).get("next_cursor") or ""
            )
            if not cursor:
                return frozenset(members)
    except Exception:  # noqa: BLE001 - routing fails closed to unassigned
        return frozenset()


def _pick(
    reviews: tuple[CandidateReview, ...], recent_states: set[str]
) -> CandidateReview | None:
    """Choose the highest-ranked ready card outside the one-state cooldown if possible."""
    ready = [item for item in reviews if item.draft is not None]
    if not ready:
        return None
    diverse = [
        item for item in ready if item.draft and item.draft.state not in recent_states
    ]
    return (diverse or ready)[0]


def _delivery_veto(
    conn: sqlite3.Connection,
    lead_id: int,
    event_id: int,
    contact_evidence_id: str,
    now: datetime,
) -> bool:
    """Use mutable state only as a final cancellation veto, never as display truth."""
    row = conn.execute(
        "SELECT status,current_event_id FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    contact = conn.execute(
        "SELECT status,expires_at FROM contact_evidence WHERE id=?",
        (contact_evidence_id,),
    ).fetchone()
    if row is None or contact is None:
        return False
    if (
        str(row["status"] or "new") != "new"
        or int(row["current_event_id"] or 0) != event_id
    ):
        return False
    return (
        str(contact["status"]) == "verified"
        and str(contact["expires_at"] or "") > now.isoformat()
    )


def run(
    client: SlackPoster | None,
    channel: str,
    conn: sqlite3.Connection,
    *,
    channel_members: frozenset[str] = frozenset(),
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> str:
    """Review, freeze, reserve, and at most once post one rich card."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    go, reason = pacing.should_post(conn, channel, at, force=force)
    if not go:
        return f"skip: {reason}"
    reviews = review_candidates(conn, channel, channel_members, now=at)
    choice = _pick(reviews, db.recent_post_states(conn, channel, 1))
    if choice is None or choice.draft is None:
        return "skip: no rich award card satisfies every evidence rule"
    if dry_run:
        preview = replace(choice.draft, fallback_text=card.fallback_text(choice.draft))
        return f"[dry-run] would post rich_award for lead #{choice.lead_id}: {preview.fallback_text}"
    if not _delivery_veto(
        conn,
        choice.draft.lead_id,
        choice.draft.event_id,
        choice.draft.contact_evidence_id,
        at,
    ):
        return "skip: candidate changed after preparation; no post attempted"
    frozen, _created = freeze(conn, choice.draft, now=at)
    prior = conn.execute(
        """SELECT 1 FROM posts WHERE snapshot_id=?
           UNION ALL SELECT 1 FROM notification_outbox WHERE snapshot_id=? LIMIT 1""",
        (frozen.id, frozen.id),
    ).fetchone()
    if prior is not None:
        return "skip: this stable award/audience delivery already exists"
    try:
        rendered = card.render(frozen)
    except Exception as exc:  # noqa: BLE001 - permanently quarantine bad render input
        delivery_key = db.reserve_notification(
            conn,
            frozen.draft.lead_id,
            frozen.draft.event_id,
            channel,
            "rich_award",
            {},
            snapshot_id=frozen.id,
        )
        if delivery_key is not None:
            db.finish_notification(
                conn, delivery_key, "unrenderable", error=type(exc).__name__
            )
        return "quarantined: rich card could not be rendered; no Slack call attempted"
    delivery_key = db.reserve_notification(
        conn,
        frozen.draft.lead_id,
        frozen.draft.event_id,
        channel,
        "rich_award",
        {"text": rendered.text, "blocks": list(rendered.blocks)},
        snapshot_id=frozen.id,
    )
    if delivery_key is None:
        return "skip: this rich-card delivery is already reserved"
    if client is None:
        db.finish_notification(conn, delivery_key, "unknown", error="missing_client")
        return "unknown: Slack client absent after reservation; no retry"
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=rendered.text,
            blocks=list(rendered.blocks),
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as exc:
        if getattr(exc.response, "status_code", None) == 200:
            db.release_notification(conn, delivery_key)
            return "error: Slack rejected the rich card; reservation released"
        db.finish_notification(conn, delivery_key, "unknown", error=type(exc).__name__)
        return "unknown: Slack delivery could not be confirmed; no retry"
    except Exception as exc:  # noqa: BLE001 - timeout is ambiguous by definition
        db.finish_notification(conn, delivery_key, "unknown", error=type(exc).__name__)
        return "unknown: Slack delivery could not be confirmed; no retry"
    slack_ts = str(response["ts"])
    try:
        db.record_post(
            conn,
            "rich_award",
            frozen.draft.lead_id,
            channel,
            slack_ts,
            frozen.draft.tier,
            delivery_key=delivery_key,
            event_id=frozen.draft.event_id,
            snapshot_id=frozen.id,
        )
        db.finish_notification(conn, delivery_key, "delivered", slack_ts=slack_ts)
        db.mark_surfaced(conn, [frozen.draft.lead_id])
        post = conn.execute(
            "SELECT id FROM posts WHERE snapshot_id=?", (frozen.id,)
        ).fetchone()
        db.record_outcome(
            conn,
            frozen.draft.lead_id,
            int(post["id"]) if post else None,
            frozen.draft.route.slack_user_id or "unassigned",
            "proactive_card_delivered",
            f"rich-delivery:{frozen.id}",
        )
    except Exception as exc:  # noqa: BLE001 - Slack already accepted the message
        try:
            db.finish_notification(conn, delivery_key, "delivered", slack_ts=slack_ts)
        except Exception:  # noqa: BLE001 - last-ditch no-repeat state
            pass
        return f"posted rich_award, but bookkeeping hit {type(exc).__name__}; no retry"
    return f"posted rich_award for lead #{frozen.draft.lead_id}: {frozen.draft.entity_name}"
