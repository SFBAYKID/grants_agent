"""Snapshot-first, reservation-before-Slack rich-card delivery.

The feature flag dispatch lives in ``cli.py``; this module assumes it was selected.
Dry-run performs only local reads and never freezes, reserves, posts, or mutates. An
ambiguous Slack outcome is finalized ``unknown`` and never blind-retried.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError

from .. import db
from .. import lead_claims
from . import card, contact_evidence, pacing, rich_actions_configured
from .preparation import CandidateReview, review_candidates
from .snapshot import award_dedup_key, freeze

_SYSTEMIC_SLACK_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "no_permission",
        "org_login_required",
        "restricted_action",
    }
)
_CONTENT_SLACK_ERRORS = frozenset(
    {
        "msg_too_long",
        "invalid_blocks",
        "invalid_block_part",
        "blocks_too_long",
        "invalid_attachments",
        "no_text",
    }
)


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
        "SELECT * FROM contact_evidence WHERE id=?",
        (contact_evidence_id,),
    ).fetchone()
    if row is None or contact is None:
        return False
    if (
        str(row["status"] or "new") != "new"
        or int(row["current_event_id"] or 0) != event_id
    ):
        return False
    # A CLAIM CAN LAND INSIDE THIS WINDOW. `review_candidates` filters claimed leads
    # out of the pool, but it ran earlier in the tick; a rep saying "I'm taking this"
    # between then and here would otherwise get the card posted at them anyway.
    if lead_claims.is_claimed(conn, lead_id):
        return False
    return (
        contact_evidence.contact_fact_is_verified(contact)
        and str(contact["expires_at"] or "") > now.isoformat()
    )


# Outcome strings shared by run() and the fallback classifier below. They MUST be the
# same objects at the return sites: the classifier matches exactly, so a reworded
# literal in run() would silently kill the fallback — a permanent cardless flag-on day
# behind a benign skip line and exit 0 (architectural-critic H1, 2026-08-05).
_SKIP_NO_ELIGIBLE_CARD = "skip: no rich award card satisfies every evidence rule"
_SKIP_CANDIDATE_CHANGED = "skip: candidate changed after preparation; no post attempted"
_SKIP_STABLE_DELIVERY_EXISTS = (
    "skip: this stable award/audience delivery already exists"
)
_SKIP_ALREADY_RESERVED = "skip: this rich-card delivery is already reserved"

# Rich outcomes after which the RESTYLED legacy daily card may still post this tick
# (Chase 2026-08-05: a card lands every weekday in the newest look — rich when the
# evidence gates pass, the restyled plain card otherwise, never silence). An outcome
# qualifies only when all three hold:
#   1. nothing was posted or reserved TODAY (both paths count posts AND outbox
#      reservations toward the same cap, so a fallback can never double-post);
#   2. no Slack call was attempted this tick (an ambiguous send must never be followed
#      by a second post); and
#   3. the rich path provably cannot post LATER today, so it is not preempted while
#      merely waiting for its slot.
# Excluded on purpose: "daily cap reached" (the day is spent), "waiting for today's
# … slot" (rich may still post), "weekend" (nothing should post), guard holds /
# blocked / backoff (a channel problem stops both paths), "unknown" (ambiguous send),
# and "error"/"quarantined" (loud failures that must keep their non-zero exit).
_FALLBACK_EXACT = frozenset(
    {
        _SKIP_NO_ELIGIBLE_CARD,
        _SKIP_CANDIDATE_CHANGED,
        _SKIP_STABLE_DELIVERY_EXISTS,
        _SKIP_ALREADY_RESERVED,
    }
)


def fallback_to_daily(outcome: str) -> bool:
    """Whether cli.cmd_drip may hand this tick to the legacy daily-card path."""
    if outcome in _FALLBACK_EXACT:
        return True
    # Derived from the same constant pacing uses, so the strings cannot drift apart.
    return outcome == (
        f"skip: missed the {pacing.HARD_CUTOFF_PT:%H:%M} Pacific hard cutoff"
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
    guard = db.channel_guard(conn, channel)
    if guard is not None:
        return f"{guard['state']}: channel hold active ({guard['last_error'] or 'unknown'})"
    go, reason = pacing.should_post(conn, channel, at, force=force)
    if not go:
        return f"skip: {reason}"
    reviews = review_candidates(conn, channel, channel_members, now=at)
    choice = _pick(reviews, db.recent_post_states(conn, channel, 1))
    if choice is None or choice.draft is None:
        return _SKIP_NO_ELIGIBLE_CARD
    if choice.draft.card_mode == "draft_ready" and not rich_actions_configured():
        return (
            "blocked: draft-ready rich card requires SLACK_WORKSPACE_ID; "
            "no post attempted"
        )
    if dry_run:
        preview = replace(
            choice.draft, fallback_text=card.fallback_text(choice.draft, at.date())
        )
        return f"[dry-run] would post rich_award for lead #{choice.lead_id}: {preview.fallback_text}"
    if not _delivery_veto(
        conn,
        choice.draft.lead_id,
        choice.draft.event_id,
        choice.draft.contact_evidence_id,
        at,
    ):
        return _SKIP_CANDIDATE_CHANGED
    frozen, _created = freeze(conn, choice.draft, now=at)
    stable_key = award_dedup_key(frozen.draft)
    prior = conn.execute(
        """SELECT 1 FROM posts WHERE snapshot_id=?
           UNION ALL SELECT 1 FROM notification_outbox WHERE snapshot_id=? LIMIT 1""",
        (frozen.id, frozen.id),
    ).fetchone()
    if prior is not None:
        return _SKIP_STABLE_DELIVERY_EXISTS
    try:
        # The tick's own clock, so the age the card prints and the pacing
        # decisions above cannot disagree about what day it is.
        rendered = card.render(frozen, at.date())
    except Exception as exc:  # noqa: BLE001 - permanently quarantine bad render input
        delivery_key = db.reserve_notification(
            conn,
            frozen.draft.lead_id,
            frozen.draft.event_id,
            channel,
            "rich_award",
            {},
            snapshot_id=frozen.id,
            stable_delivery_key=stable_key,
        )
        if delivery_key is not None:
            db.finish_notification(
                conn, delivery_key, "unrenderable", error=type(exc).__name__
            )
        return "quarantined: rich card could not be rendered; no Slack call attempted"
    slot_key = f"rich-slot:{channel}:{stable_key}"
    if not pacing.reserve_daily_slot(conn, channel, at, "rich_award", slot_key):
        return "skip: daily cap reached (1)"
    delivery_key = db.reserve_notification(
        conn,
        frozen.draft.lead_id,
        frozen.draft.event_id,
        channel,
        "rich_award",
        {"text": rendered.text, "blocks": list(rendered.blocks)},
        snapshot_id=frozen.id,
        stable_delivery_key=stable_key,
    )
    if delivery_key is None:
        pacing.release_daily_slot(conn, slot_key)
        return _SKIP_ALREADY_RESERVED
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
        if getattr(exc.response, "status_code", None) == 429:
            db.release_notification(conn, delivery_key)
            pacing.release_daily_slot(conn, slot_key)
            until = (at + timedelta(minutes=1)).isoformat()
            db.set_channel_guard(
                conn, channel, "backoff", "ratelimited", available_at=until
            )
            return "backoff: Slack rate limit active; no lead consumed"
        if getattr(exc.response, "status_code", None) == 200:
            code = str(exc.response.get("error") or "unknown_error")
            if code in _SYSTEMIC_SLACK_ERRORS:
                db.release_notification(conn, delivery_key)
                pacing.release_daily_slot(conn, slot_key)
                until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                db.set_channel_guard(conn, channel, "blocked", code, available_at=until)
                return (
                    f"blocked: Slack rejected this channel ({code}); no lead consumed"
                )
            if code in _CONTENT_SLACK_ERRORS:
                db.finish_notification(conn, delivery_key, "rejected", error=code)
                return f"quarantined: Slack rejected this rich card ({code})"
            db.release_notification(conn, delivery_key)
            pacing.release_daily_slot(conn, slot_key)
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
