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


def delivery_attempts_today(
    conn: sqlite3.Connection, channel: str, now_utc: datetime | None = None
) -> list[sqlite3.Row]:
    """Today's proactive delivery RESERVATIONS for one channel, Pacific.

    The fail-closed counterpart to `posts_today`. `reserve_notification` writes its row
    BEFORE the Slack call, whereas `record_post` writes AFTER it — so if the post
    succeeds and the bookkeeping then fails (full disk, lock, CHECK violation), `posts`
    has no row while the outbox does.

    That gap is not theoretical: every cap in `drip.pacing_ok` is derived from a count
    of today's posts, so a single missing `posts` row makes the daily cap, the absolute
    cap AND the minimum-gap rule all read zero. The next tick would then post again, and
    the one after that, walking down the 544-lead pool one card every 30 minutes until
    the window closed. Counting reservations too means a confirmed send is remembered
    even when recording it failed.

    `lead_id IS NOT NULL` is load-bearing, not tidiness: channel-guard rows live in this
    same table with a NULL lead_id, and without the filter a guard counted as a
    delivery — verified to produce `daily cap reached (1)` with zero posts and zero
    reservations, silently spending the day's only card.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    local_date = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).date()
    start_local = datetime.combine(
        local_date, time.min, tzinfo=ZoneInfo("America/Los_Angeles")
    )
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    return list(
        conn.execute(
            """SELECT * FROM notification_outbox
               WHERE audience=? AND lead_id IS NOT NULL
                 AND created_at>=? AND created_at<?
               ORDER BY created_at, id""",
            (channel, start_utc.isoformat(), end_utc.isoformat()),
        )
    )


def recent_post_states(
    conn: sqlite3.Connection, channel: str, limit: int
) -> set[str]:
    """Return the distinct states of the most recent `limit` proactive posts in a channel.

    Feeds the drip state-diversity cooldown ([[grant-drip-campaign-direction]]): a state
    posted within this window is de-prioritised so the daily card rotates across the
    nationwide cohort instead of clustering. Empty when there is no history.
    """
    if limit <= 0:
        return set()
    # The states of the last `limit` POSTS (not the last `limit` distinct states) — a
    # DISTINCT + LIMIT would collapse states first and limit the wrong thing.
    rows = conn.execute(
        """SELECT l.state FROM posts p JOIN leads l ON l.id=p.lead_id
           WHERE p.channel=? AND p.lead_id IS NOT NULL
           ORDER BY p.posted_at DESC, p.id DESC LIMIT ?""",
        (channel, limit),
    ).fetchall()
    return {str(r[0] or "") for r in rows}


def nugget_candidates(
    conn: sqlite3.Connection, channel: str
) -> list[sqlite3.Row]:
    """Unsurfaced GOLD leads eligible for a drip nugget.

    NOTE the deliberate absence of `e.suppressed=0` (Chase, 2026-07-22 — "gold is what
    we should really be serving users each day"). `suppressed` is set by
    `db.upsert_lead` from `RawItem.backfill`, which every award poller sets for anything
    obligated (or, lacking a date, merely published) more than 90 days ago. That flag
    was a first-rollout guard against a notification wave. Measured on production
    2026-07-22 it had become a permanent gag: 638 of 638 gold leads carried
    `suppressed=1`, this query returned 0 rows on every tick, and `drip.pick()` fell
    past GOLD to a silver RFP every single day. The wave it was written to prevent is
    already prevented by `drip.DAILY_CAP = 1` — one card a day cannot be a wave.

    The award-event filter below is what keeps this honest: only a *verified* award
    event can surface, `drip.build_nugget` states no date it was not given, and
    `scoring.lead_score` ranks a dated recent award far above an undated one, so the
    freshest evidence still leads.

    Leads already in `posts` are excluded outright. `upsert_lead` resets a lead's status
    to 'new' whenever a new unsuppressed event lands, which would otherwise re-open an
    already-posted lead for a second card — the exact repeat-in-the-channel class of bug
    Chase reported on 2026-07-22.

    Leads present in `notification_outbox` are excluded for a DIFFERENT and more
    dangerous reason (architectural-critic, 2026-07-22, reproduced against a real DB).
    When a Slack send is ambiguous — 5xx, ratelimited, socket timeout — the row is left
    in state 'unknown' and deliberately never retried, because the message may in fact
    have been delivered. But the lead then stays `status='new'`, absent from `posts`,
    and still the winner of `_best_nugget`'s deterministic `max()` over a static pool.
    Every later tick re-picked that same lead, `reserve_notification` hit the existing
    delivery_key and returned None, and `run_drip` returned early — BEFORE ever reaching
    the RFP or bulletin tiers. One ambiguous send therefore silenced the entire product,
    permanently, with a benign-looking `skip:` line and exit code 0. Excluding reserved
    leads keeps the never-blind-retry guarantee (that lead stays skipped) while letting
    the queue advance to the next one.

    The `amount > 0` filter is a WEDGE GUARD, not a quality rule. `drip._award_facts`
    raises on a missing or non-positive amount, and `cli.cmd_drip` has no handler, so an
    amountless gold lead reaching the top of `_best_nugget` would crash the tick before
    anything is posted or marked surfaced — and then be picked again, identically, on
    every tick forever. Silent, permanent, and indistinguishable from the outage this
    query was just changed to fix. `scoring.grade` should never mint such a lead, but
    the renderer's precondition belongs in the query that feeds it.
    """
    return list(
        conn.execute(
            f"""SELECT {LEAD_EVENT_SELECT}, {CRM_CONTEXT_SELECT}
            FROM leads l
            JOIN funding_events e ON e.id=l.current_event_id
            WHERE l.lead_grade='gold' AND l.status='new'
              AND e.verification_status='verified'
              AND e.event_type IN ('award_announced','award_obligated')
              AND l.amount IS NOT NULL AND l.amount > 0
              AND l.id NOT IN (SELECT lead_id FROM posts
                               WHERE lead_id IS NOT NULL AND channel=?)
              AND l.id NOT IN (SELECT lead_id FROM notification_outbox
                               WHERE lead_id IS NOT NULL AND audience=?)""",
            (channel, channel),
        )
    )


def rfp_candidates(conn: sqlite3.Connection, channel: str) -> list[sqlite3.Row]:
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
              AND l.id NOT IN (SELECT lead_id FROM posts
                               WHERE lead_id IS NOT NULL AND channel=?)
              AND l.id NOT IN (SELECT lead_id FROM notification_outbox
                               WHERE lead_id IS NOT NULL AND audience=?)
              AND l.funds_end != '' AND date(l.funds_end) >= date('now')
            ORDER BY date(l.funds_end) ASC, l.id""",
            (channel, channel),
        )
    )


def bulletin_candidates(
    conn: sqlite3.Connection, channel: str, max_age_days: int = 14
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
              AND l.id NOT IN (SELECT lead_id FROM posts
                               WHERE lead_id IS NOT NULL AND channel=?)
              AND l.id NOT IN (SELECT lead_id FROM notification_outbox
                               WHERE lead_id IS NOT NULL AND audience=?)
              AND l.funds_end != '' AND date(l.funds_end) >= date('now')
            ORDER BY date(l.funds_end) ASC,l.id""",
            (f"-{max_age_days} days", channel, channel),
        )
    )
