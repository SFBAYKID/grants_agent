"""Human reward signals and the drip-selection queries.

Split out of db.py to respect the 1000-line cap. Two responsibilities: recording what
humans did with a post (reactions, replies, outcomes — the tuning signal Grant scores
against) and choosing what is eligible to surface next (award nuggets, open RFPs,
program bulletins). Shared row shapes come from db_common so this never imports db.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .db_common import CRM_CONTEXT_SELECT, LEAD_EVENT_SELECT, _now

def record_engagement(
    conn: sqlite3.Connection, post_id: int, slack_user: str, kind: str
) -> bool:
    """+1 point when a human interacts with a post. Deduped per (post, user, kind)
    so one enthusiastic user can't inflate the score. Returns True if new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO engagement (post_id, slack_user, kind, at) "
        "VALUES (?,?,?,?)",
        (post_id, slack_user, kind, _now()),
    )
    conn.commit()
    if cur.rowcount == 1:
        post = conn.execute(
            "SELECT lead_id FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        if post is not None:
            record_outcome(
                conn,
                int(post["lead_id"]) if post["lead_id"] is not None else None,
                post_id,
                slack_user,
                kind,
                f"engagement:{post_id}:{slack_user}:{kind}",
            )
    return cur.rowcount == 1


_OUTCOME_POINTS = {
    "reaction": 1,
    "reply": 2,
    "question": 2,
    "snoozed": -2,
    "bad_lead": -8,
    "contacted": 6,
    "campaign_added": 8,
}


def record_outcome(
    conn: sqlite3.Connection,
    lead_id: int | None,
    post_id: int | None,
    slack_user: str,
    kind: str,
    source_key: str,
) -> bool:
    """Persist one deduplicated human reward signal with an explicit point weight."""
    if kind not in _OUTCOME_POINTS:
        raise ValueError(f"unsupported outcome kind '{kind}'")
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO outcome_events
                 (id,lead_id,post_id,slack_user,kind,points,source_key,occurred_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                lead_id,
                post_id,
                slack_user,
                kind,
                _OUTCOME_POINTS[kind],
                source_key,
                _now(),
            ),
        )
    return cur.rowcount == 1


def program_outcome_points(conn: sqlite3.Connection, program: str) -> list[int]:
    """Return verified human outcome weights for one exact program label."""
    return [
        int(row[0])
        for row in conn.execute(
            """SELECT o.points FROM outcome_events o
           JOIN leads l ON l.id=o.lead_id
           WHERE UPPER(COALESCE(l.program,''))=UPPER(?)""",
            (program or "",),
        )
    ]


def engagement_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Grant's score: total points + per-kind breakdown (the tuning signal)."""
    stats = {"total": conn.execute("SELECT COUNT(*) FROM engagement").fetchone()[0]}
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM engagement GROUP BY kind"):
        stats[kind] = n
    return stats


def posts_today(
    conn: sqlite3.Connection, channel: str, now_utc: datetime | None = None
) -> list[sqlite3.Row]:
    """Today's proactive posts in Pacific time, where the Slack team operates."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local_date = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).date()
    start_local = datetime.combine(
        local_date, time.min, tzinfo=ZoneInfo("America/Los_Angeles")
    )
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    return list(
        conn.execute(
            """SELECT * FROM posts WHERE channel=? AND posted_at>=? AND posted_at<?
           ORDER BY posted_at,id""",
            (channel, start_utc.isoformat(), end_utc.isoformat()),
        )
    )


def nugget_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unsurfaced GOLD leads eligible for a drip nugget."""
    return list(
        conn.execute(
            f"""SELECT {LEAD_EVENT_SELECT}, {CRM_CONTEXT_SELECT}
            FROM leads l
            JOIN funding_events e ON e.id=l.current_event_id
            WHERE l.lead_grade='gold' AND l.status='new' AND e.suppressed=0
              AND e.verification_status='verified'
              AND e.event_type IN ('award_announced','award_obligated')"""
        )
    )


def rfp_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Open, unsurfaced physical-security RFP leads for a proactive alert.

    An open RFP (verified future deadline) for cameras/access control is an active
    buyer, so these are surfaced individually and promptly (Chase, 2026-07-18) — the
    soonest deadline first. Already-posted leads are excluded so nothing repeats.
    """
    return list(
        conn.execute(
            f"""SELECT {LEAD_EVENT_SELECT} FROM leads l
            JOIN funding_events e ON e.id=l.current_event_id
            WHERE l.source='rfp' AND l.lead_grade='silver'
              AND e.suppressed=0 AND e.verification_status='verified'
              AND e.event_type='rfp_posted'
              AND l.id NOT IN (SELECT lead_id FROM posts WHERE lead_id IS NOT NULL)
              AND l.funds_end != '' AND date(l.funds_end) >= date('now')
            ORDER BY date(l.funds_end) ASC, l.id"""
        )
    )


def bulletin_candidates(
    conn: sqlite3.Connection, max_age_days: int = 14
) -> list[sqlite3.Row]:
    """Return fresh federal or California application-window bulletins.

    These are program-level signals rather than award evidence. The earliest
    verified closing date sorts first so users see the most time-sensitive item.
    """
    return list(
        conn.execute(
            f"""SELECT {LEAD_EVENT_SELECT} FROM leads l
            JOIN funding_events e ON e.id=l.current_event_id
            WHERE l.source IN ('grants.gov','ca-grants-portal')
              AND l.first_seen >= datetime('now', ?)
              AND e.suppressed=0 AND e.verification_status='verified'
              AND e.event_type='application_window_opened'
              AND l.id NOT IN (SELECT lead_id FROM posts WHERE lead_id IS NOT NULL)
              AND l.funds_end != '' AND date(l.funds_end) >= date('now')
            ORDER BY date(l.funds_end) ASC,l.id""",
            (f"-{max_age_days} days",),
        )
    )
