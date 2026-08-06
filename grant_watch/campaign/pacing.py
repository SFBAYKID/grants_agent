"""Deterministic one-card weekday pacing for the rich campaign.

The one daily slot is 10:00–10:45 Pacific and the hard cutoff is 11:30 Pacific. A
missed slot never posts in the afternoon. Existing posts and pre-Slack reservations
share the same cap, so neither legacy nor future reminder paths can bypass it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .. import db

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

# Exclusive hard cutoff for the day's rich card, exported so delivery's skip string and
# cli's fallback classification stay derived from ONE value. It must sit at least one
# full cron interval past the END of the slot band: with the cutoff at 11:00, a slot of
# 10:31–10:45 was unreachable on a :00/:30 30-minute cron grid — the first tick at or
# after the slot was 11:00, which the cutoff refused — so those days could never post a
# rich card at all. 11:29 is still late morning, so the invariant "a missed slot never
# posts in the afternoon" holds.
HARD_CUTOFF_PT = time(11, 30)


def daily_slot(channel: str, now_utc: datetime) -> datetime:
    """Return the stable per-channel/day Pacific slot between 10:00 and 10:45."""
    local = now_utc.astimezone(PT)
    seed = f"rich-award-v1|{channel}|{local.date().isoformat()}"
    minute = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 46
    return datetime.combine(local.date(), time(10, minute), tzinfo=PT)


def reserve_daily_slot(
    conn: sqlite3.Connection,
    channel: str,
    now_utc: datetime,
    delivery_kind: str,
    delivery_key: str,
) -> bool:
    """Atomically claim the channel's single Pacific-day proactive delivery slot."""
    local_date = now_utc.astimezone(PT).date().isoformat()
    with conn:
        inserted = conn.execute(
            """INSERT OR IGNORE INTO proactive_daily_slots
                 (audience,local_date,delivery_kind,delivery_key,reserved_at)
               VALUES (?,?,?,?,?)""",
            (
                channel,
                local_date,
                delivery_kind,
                delivery_key,
                now_utc.isoformat(),
            ),
        ).rowcount
    return inserted == 1


def release_daily_slot(conn: sqlite3.Connection, delivery_key: str) -> None:
    """Release a slot only when it is proven that no Slack post was attempted."""
    with conn:
        conn.execute(
            "DELETE FROM proactive_daily_slots WHERE delivery_key=?", (delivery_key,)
        )


def _daily_slot_count(conn: sqlite3.Connection, channel: str, now_utc: datetime) -> int:
    """Return whether this Pacific day already has an atomic proactive claim."""
    local_date = now_utc.astimezone(PT).date().isoformat()
    row = conn.execute(
        """SELECT COUNT(*) FROM proactive_daily_slots
           WHERE audience=? AND local_date=?""",
        (channel, local_date),
    ).fetchone()
    return int(row[0])


def should_post(
    conn: sqlite3.Connection,
    channel: str,
    now_utc: datetime,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Enforce weekday, deterministic slot, hard cutoff, and one total post."""
    count = max(
        len(db.posts_today(conn, channel, now_utc)),
        len(db.delivery_attempts_today(conn, channel, now_utc)),
        _daily_slot_count(conn, channel, now_utc),
    )
    if count >= 1:
        return False, "daily cap reached (1)"
    if force:
        return True, "forced"
    pt = now_utc.astimezone(PT)
    et = now_utc.astimezone(ET)
    if pt.weekday() >= 5 or et.weekday() >= 5:
        return False, "weekend"
    if pt.time() >= HARD_CUTOFF_PT:
        return False, f"missed the {HARD_CUTOFF_PT:%H:%M} Pacific hard cutoff"
    slot = daily_slot(channel, now_utc)
    if pt < slot:
        return False, f"waiting for today's {slot.strftime('%H:%M')} Pacific slot"
    return True, "ready"
