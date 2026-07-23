"""Deterministic one-card weekday pacing for the rich campaign.

The one daily slot is 10:00–10:45 Pacific and the hard cutoff is 11:00 Pacific. A
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


def daily_slot(channel: str, now_utc: datetime) -> datetime:
    """Return the stable per-channel/day Pacific slot between 10:00 and 10:45."""
    local = now_utc.astimezone(PT)
    seed = f"rich-award-v1|{channel}|{local.date().isoformat()}"
    minute = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 46
    return datetime.combine(local.date(), time(10, minute), tzinfo=PT)


def should_post(
    conn: sqlite3.Connection,
    channel: str,
    now_utc: datetime,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Enforce weekday, deterministic slot, hard cutoff, and one total post."""
    if force:
        return True, "forced"
    pt = now_utc.astimezone(PT)
    et = now_utc.astimezone(ET)
    if pt.weekday() >= 5 or et.weekday() >= 5:
        return False, "weekend"
    if pt.time() >= time(11, 0):
        return False, "missed the 11:00 Pacific hard cutoff"
    slot = daily_slot(channel, now_utc)
    if pt < slot:
        return False, f"waiting for today's {slot.strftime('%H:%M')} Pacific slot"
    count = max(
        len(db.posts_today(conn, channel, now_utc)),
        len(db.delivery_attempts_today(conn, channel, now_utc)),
    )
    if count >= 1:
        return False, "daily cap reached (1)"
    return True, "ready"
