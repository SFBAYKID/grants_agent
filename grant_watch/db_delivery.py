"""Proactive delivery state: reservations, quarantines, and channel guards.

Split out of db.py at the 1000-line cap (Constitution rule 4). One responsibility:
everything that records what Grant TRIED to send and why it did or did not land.

The core invariant lives here. `reserve_notification` writes BEFORE the Slack call and
`record_post` writes after it, so the reservation is the only signal that cannot be
missing for a delivered message — which is why the drip caps and the candidate
exclusions are keyed off it rather than off `posts`.

Row states: `sending` (reserved, outcome unknown), `delivered`, `unknown` (ambiguous —
may have landed, never auto-retried), `rejected` (Slack refused THIS card), and
`unrenderable` (the card could not be built at all, so no Slack call was made).
A NULL `lead_id` marks a channel-level guard (`blocked` / `backoff`) rather than a lead.
"""

from __future__ import annotations

import json
import sqlite3

from .db_common import _now


def reserve_notification(
    conn: sqlite3.Connection,
    lead_id: int,
    event_id: int | None,
    channel: str,
    delivery_class: str,
    payload: dict[str, object],
) -> str | None:
    """Atomically reserve one event/channel delivery before calling Slack.

    A stale ``sending`` state is intentionally not retried automatically: a network
    timeout can mean Slack accepted the post, so blind retrying could duplicate it.
    """
    delivery_key = f"{channel}:lead:{lead_id}:event:{event_id or 'projection'}"
    now = _now()
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO notification_outbox
                 (delivery_key,event_id,lead_id,audience,delivery_class,payload_json,
                  state,attempts,available_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'sending',1,?,?,?)""",
            (
                delivery_key,
                event_id,
                lead_id,
                channel,
                delivery_class,
                json.dumps(payload, sort_keys=True),
                now,
                now,
                now,
            ),
        )
    return delivery_key if cur.rowcount == 1 else None


def finish_notification(
    conn: sqlite3.Connection,
    delivery_key: str,
    state: str,
    slack_ts: str = "",
    error: str = "",
) -> None:
    """Finalize a reserved Slack delivery.

    States: `delivered` (confirmed), `unknown` (ambiguous — may or may not have landed,
    never auto-retried), `rejected` (Slack answered and refused THIS card, so it
    provably did not land and must not be retried), `unrenderable` (the card could not
    be built from the lead's data at all — no Slack call was ever made).

    `rejected` and `unrenderable` are durable quarantines: the lead stays excluded from
    the candidate queries, but the row records WHY, so `cli drip-blocked` can show a
    human what was set aside instead of it vanishing silently.
    """
    if state not in {"delivered", "unknown", "rejected", "unrenderable"}:
        raise ValueError(f"unsupported notification state '{state}'")
    with conn:
        conn.execute(
            """UPDATE notification_outbox
               SET state=?,slack_ts=?,last_error=?,updated_at=? WHERE delivery_key=?""",
            (state, slack_ts or None, error or None, _now(), delivery_key),
        )


def release_notification(conn: sqlite3.Connection, delivery_key: str) -> None:
    """Delete a reservation for a card that provably did NOT reach Slack.

    Used only when Slack answered and refused (HTTP 200 + an error code): the message
    did not land, so the lead must go back in the pool rather than be consumed. Leaving
    the row would permanently destroy a good lead — measured behavior before this
    existed was 1-2 gold leads burned per weekday under a revoked token, with nothing
    posted and nothing reported.
    """
    with conn:
        conn.execute(
            "DELETE FROM notification_outbox WHERE delivery_key=?", (delivery_key,)
        )


def quarantine_lead(
    conn: sqlite3.Connection,
    lead_id: int,
    event_id: int | None,
    channel: str,
    delivery_class: str,
    reason: str,
) -> str:
    """Durably set a lead aside when its card cannot be built at all.

    The renderers raise before any Slack call, so no reservation exists to finalize —
    which meant the picker chose the same unrenderable lead on every tick, crashed, and
    silenced the product permanently while writing nothing anywhere. Recording the
    failure as an outbox row makes the existing exclusion do the work AND leaves an
    operator-visible trace of what was dropped and why.
    """
    delivery_key = f"{channel}:lead:{lead_id}:event:{event_id or 'projection'}"
    now = _now()
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO notification_outbox
                 (delivery_key,event_id,lead_id,audience,delivery_class,payload_json,
                  state,attempts,available_at,created_at,updated_at,last_error)
               VALUES (?,?,?,?,?,?, 'unrenderable',0,?,?,?,?)""",
            (
                delivery_key,
                event_id,
                lead_id,
                channel,
                delivery_class,
                "{}",
                now,
                now,
                now,
                reason[:500],
            ),
        )
    return delivery_key


def _channel_guard_key(channel: str) -> str:
    """The reserved delivery_key holding a channel-level block or backoff."""
    return f"channel-guard:{channel}"


def set_channel_guard(
    conn: sqlite3.Connection,
    channel: str,
    state: str,
    reason: str,
    available_at: str = "",
) -> None:
    """Record a channel-wide condition that must stop the drip for this audience.

    `state='blocked'` is a persistent operator-cleared stop, used when Slack tells us
    the CHANNEL or the CREDENTIALS are wrong (`channel_not_found`, `invalid_auth`, …).
    Retrying cannot help, and every retry previously burned a lead. `state='backoff'`
    is self-clearing at `available_at`, used for rate limiting.

    Stored as a `notification_outbox` row with a NULL `lead_id`, so it is invisible to
    the candidate exclusions (which all require `lead_id IS NOT NULL`) but visible to
    `blocked_notifications` and therefore to `cli drip-blocked`.
    """
    if state not in {"blocked", "backoff"}:
        raise ValueError(f"unsupported channel guard state '{state}'")
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO notification_outbox
                 (delivery_key,event_id,lead_id,audience,delivery_class,payload_json,
                  state,attempts,available_at,created_at,updated_at,last_error)
               VALUES (?,NULL,NULL,?,'channel-guard','{}',?,0,?,?,?,?)
               ON CONFLICT(delivery_key) DO UPDATE SET
                 state=excluded.state, available_at=excluded.available_at,
                 updated_at=excluded.updated_at, last_error=excluded.last_error,
                 attempts=notification_outbox.attempts+1""",
            (
                _channel_guard_key(channel),
                channel,
                state,
                available_at or now,
                now,
                now,
                reason[:500],
            ),
        )


def channel_guard(conn: sqlite3.Connection, channel: str) -> sqlite3.Row | None:
    """Return an ACTIVE channel guard, or None. A lapsed backoff clears itself."""
    row = conn.execute(
        "SELECT * FROM notification_outbox WHERE delivery_key=?",
        (_channel_guard_key(channel),),
    ).fetchone()
    if row is None:
        return None
    if str(row["state"]) == "backoff" and str(row["available_at"] or "") <= _now():
        clear_channel_guard(conn, channel)
        return None
    return row


def clear_channel_guard(conn: sqlite3.Connection, channel: str) -> bool:
    """Remove a channel guard. Returns whether one was present."""
    with conn:
        cur = conn.execute(
            "DELETE FROM notification_outbox WHERE delivery_key=?",
            (_channel_guard_key(channel),),
        )
    return cur.rowcount > 0


def blocked_notifications(
    conn: sqlite3.Connection, channel: str = ""
) -> list[sqlite3.Row]:
    """Leads set aside and never delivered — the operator-visible failure surface.

    Every non-delivered outbox row is a lead permanently excluded from the pool. With no
    way to list them, silent inventory loss looks identical to a quiet week.
    """
    sql = """SELECT o.id, o.delivery_key, o.lead_id, o.audience, o.state, o.last_error,
                    o.created_at, o.updated_at, l.entity_name, l.state AS lead_state
             FROM notification_outbox o
             LEFT JOIN leads l ON l.id = o.lead_id
             WHERE o.state != 'delivered'"""
    params: tuple[str, ...] = ()
    if channel:
        sql += " AND o.audience=?"
        params = (channel,)
    return list(conn.execute(sql + " ORDER BY o.created_at DESC, o.id DESC", params))


