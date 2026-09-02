"""The daily list: the freshest awards on file, as one card per lead.

WHY THIS REPLACED THE SINGLE DAILY CARD (Chase, 2026-09-01). A rep phoned a district
ten months after its award and was told a competitor had already finished the job.
Measured afterwards, EVERY award card this product had ever posted was between 277 and
653 days old, median 301, and the entire eligible pool sat on one date. One card a day
could not drain that, and the card did not even say how old the money was.

Chase's rule, in his words: "a daily list of ... the newest freshest stuff. If for some
reason we have already posted the data from a previous day then we slowly go back but
we are always checking for fresh data."

WALKING BACKWARDS IS NOT IMPLEMENTED, AND THAT IS THE POINT. Order by award date
descending, skip what this channel has already been shown, take the top N. Repeats are
impossible because `daily_list_items` carries `UNIQUE(channel, lead_id)`, so when fresh
material runs out the next-newest unseen lead is simply an older one. There is no
backfill mode to get wrong, and no pointer that can be left in the wrong place.

EVERY ROW STATES ITS AGE. `presentation.award_age_phrase` exists because of the call
above; a date a rep has to do arithmetic on is not the same as being told the money is
eleven months gone.

WHAT THIS DELIBERATELY DOES NOT DO, so nobody reads capability into silence:
  * it writes NO `posts` row, so it produces no `card_unengaged` nudge and no
    `card_escalated` escalation. At 25 leads a day the follow-up system's own
    arithmetic breaks — its lookback covers ~2.4 days while a nudge comes due at 1 —
    so >96% of subjects would age out unsent, invisibly. Follow-ups for list items are
    unbuilt, on purpose, rather than half-built and appearing to run.
  * it does not research contacts. Measured on this cohort the finder verified 1
    contact in 19 organizations, so a contact line on every row would be 24 blanks.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ..db_common import LEAD_EVENT_SELECT, UNCLAIMED_LEAD_PREDICATE
from .daily_list_card import build_blocks, notification_text

# Chase, 2026-09-01, chosen over 5 and 10. Worth knowing what it implies: the pool this
# draws from is finite, so 25/day drains a backlog rather than tracking an inflow.
DEFAULT_LIST_SIZE = 25

# Only an award a human would call an award. `record_observed` means Grant first SAW a
# row, not that anything happened that day, and putting one on a list headed "newest
# awards" would be a claim the evidence does not make.
_AWARD_EVENTS = ("award_obligated", "award_announced")


def candidates(
    conn: sqlite3.Connection, channel: str, limit: int = DEFAULT_LIST_SIZE
) -> list[sqlite3.Row]:
    """The freshest unseen awards for one channel, newest first.

    `occurred_on` MUST be present. An undated award cannot be placed in a list whose
    entire claim is recency, and `scoring.grade` already sends undated awards to
    SILVER for the same reason. `amount > 0` is the renderer's precondition, carried
    in the query that feeds it — the lesson `nugget_candidates` records at length.

    `id DESC` breaks ties so a repeated run returns the SAME rows: 17 of the freshest
    25 in production share one `occurred_on`, and without a total order the selection
    is nondeterministic.
    """
    placeholders = ",".join("?" for _ in _AWARD_EVENTS)
    return list(
        conn.execute(
            f"""SELECT {LEAD_EVENT_SELECT} FROM leads l
                JOIN funding_events e ON e.id=l.current_event_id
                WHERE COALESCE(l.status,'new') != 'dead'
                  AND e.verification_status='verified'
                  AND e.event_type IN ({placeholders})
                  AND e.occurred_on IS NOT NULL AND e.occurred_on != ''
                  AND l.amount IS NOT NULL AND l.amount > 0
                  AND l.id NOT IN (SELECT lead_id FROM daily_list_items
                                   WHERE channel=?)
                  AND l.id NOT IN (SELECT lead_id FROM posts
                                   WHERE lead_id IS NOT NULL AND channel=?)
                  AND {UNCLAIMED_LEAD_PREDICATE}
                ORDER BY date(e.occurred_on) DESC, l.id DESC
                LIMIT ?""",
            (*_AWARD_EVENTS, channel, channel, limit),
        )
    )


def already_listed_today(conn: sqlite3.Connection, channel: str, today: date) -> bool:
    """Whether this channel has already had its list today.

    The cap is one list per channel per day, so a cron that ticks more than once — or
    a retry after a timeout — cannot post a second one.
    """
    row = conn.execute(
        "SELECT 1 FROM daily_list_items WHERE channel=? AND listed_on=? LIMIT 1",
        (channel, today.isoformat()),
    ).fetchone()
    return row is not None


def _reserve(
    conn: sqlite3.Connection,
    channel: str,
    rows: list[sqlite3.Row],
    now: datetime,
) -> None:
    """Claim these leads for this channel BEFORE the Slack call.

    Reserving first is what makes a crash between send and record safe: the leads are
    already spoken for, so a second run cannot list them again. `UNIQUE(channel,
    lead_id)` makes a concurrent second run raise rather than duplicate.
    """
    with conn:
        conn.executemany(
            """INSERT INTO daily_list_items
                 (channel,lead_id,rank,state,listed_on,listed_at)
               VALUES (?,?,?,'reserved',?,?)""",
            [
                (
                    channel,
                    int(row["id"]),
                    rank,
                    now.astimezone(timezone.utc).date().isoformat(),
                    now.isoformat(),
                )
                for rank, row in enumerate(rows, start=1)
            ],
        )


def _finish(
    conn: sqlite3.Connection,
    channel: str,
    lead_ids: list[int],
    *,
    state: str,
    slack_ts: str = "",
) -> None:
    """Mark a reservation delivered, or release it so the leads are not burned."""
    placeholders = ",".join("?" for _ in lead_ids)
    with conn:
        if state == "release":
            conn.execute(
                f"""DELETE FROM daily_list_items
                     WHERE channel=? AND state='reserved'
                       AND lead_id IN ({placeholders})""",
                (channel, *lead_ids),
            )
            return
        conn.execute(
            f"""UPDATE daily_list_items SET state=?, slack_ts=?
                 WHERE channel=? AND state='reserved'
                   AND lead_id IN ({placeholders})""",
            (state, slack_ts or None, channel, *lead_ids),
        )


# Slack refusals that describe the CHANNEL or the CREDENTIALS rather than this message.
# Retrying cannot help, and the leads must go back in the pool rather than be spent on
# a message nobody could have received.
_RELEASE_ERRORS = frozenset(
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
        "msg_too_long",
        "invalid_blocks",
        "invalid_block_part",
        "blocks_too_long",
        "no_text",
    }
)


def run(
    client: WebClient | None,
    channel: str,
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_LIST_SIZE,
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> str:
    """Post one day's list. Returns a one-line outcome for cron.log."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = at.date()
    if not force and already_listed_today(conn, channel, today):
        return "skip: this channel already had its list today"
    rows = candidates(conn, channel, limit)
    if not rows:
        return "skip: nothing unseen to list"

    if dry_run:
        head = ", ".join(str(row["entity_name"]) for row in rows[:3])
        return f"[dry-run] would list {len(rows)} awards for {channel}: {head}…"

    lead_ids = [int(row["id"]) for row in rows]
    _reserve(conn, channel, rows, at)
    blocks = build_blocks(rows, today)
    text = notification_text(rows)
    if client is None:
        _finish(conn, channel, lead_ids, state="release")
        return "error: no Slack client configured; nothing listed"
    try:
        result = client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as exc:
        code = str((exc.response or {}).get("error", ""))
        if code in _RELEASE_ERRORS:
            _finish(conn, channel, lead_ids, state="release")
            return f"error: Slack refused the list ({code}); leads released"
        # AMBIGUOUS — a 5xx, a timeout, a ratelimit. The message may have arrived, so
        # the reservation STAYS and is never retried. A second copy of somebody's
        # daily list is worse than none.
        _finish(conn, channel, lead_ids, state="unknown")
        return f"unknown: Slack outcome for the list is ambiguous ({code})"
    except Exception as exc:  # noqa: BLE001 — same ambiguity, without a Slack code
        _finish(conn, channel, lead_ids, state="unknown")
        return f"unknown: the list send failed ({type(exc).__name__})"

    _finish(
        conn, channel, lead_ids, state="delivered", slack_ts=str(result.get("ts") or "")
    )
    return f"posted a list of {len(rows)} awards to {channel}"
