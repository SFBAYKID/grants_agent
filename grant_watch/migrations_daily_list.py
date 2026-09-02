"""Forward-only schema for the daily freshest-awards list.

WHY A SEPARATE LEDGER AND NOT `posts`. `posts` carries `UNIQUE(channel, ts)`
(`migrations_rich.py`), so one Slack message can own exactly one row. A list of 25
leads is one message, so it cannot write 25 `posts` rows — the constraint would raise
AFTER `chat_postMessage` already succeeded, which is the migration-13 wedge this repo
has paid for twice. Writing ONE `posts` row instead is worse: every "already posted"
exclusion in the product is `l.id NOT IN (SELECT lead_id FROM posts …)`, so 24 of the
25 leads would be invisible to it and would come back tomorrow, and the day after.

`UNIQUE(channel, lead_id)` IS THE WHOLE FEATURE. Chase's rule was "if we have already
posted the data from a previous day then we slowly go back but we are always checking
for fresh data". Walking backwards needs no logic at all once repeats are impossible:
order by award date descending, skip what this channel has already seen, take the top
N. When fresh material runs out the next-newest unseen lead is by definition older, so
the list works back through history on its own.

THE STATE COLUMN EXISTS FOR THE AMBIGUOUS SEND. Rows are reserved BEFORE the Slack
call. A clean success marks them `delivered`; a definite refusal releases them so the
leads are not burned; and a 5xx, a timeout or a ratelimit leaves them `unknown` and
they are never retried — the message may in fact have been delivered, and posting a
second copy of somebody's daily list is worse than posting none.
"""

from __future__ import annotations

import sqlite3

LIST_ITEM_STATES = ("reserved", "delivered", "unknown")


def migration_49_daily_list(conn: sqlite3.Connection) -> None:
    """Create the per-lead ledger of what each channel's daily list has shown."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_list_items (
              id INTEGER PRIMARY KEY,
              channel TEXT NOT NULL,
              lead_id INTEGER NOT NULL REFERENCES leads(id),
              rank INTEGER NOT NULL,
              state TEXT NOT NULL,
              slack_ts TEXT,
              listed_on TEXT NOT NULL,
              listed_at TIMESTAMP NOT NULL,
              UNIQUE(channel, lead_id)
           )"""
    )
    # "Has this channel had a list today?" — the pacing question, asked every tick.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_daily_list_day
           ON daily_list_items(channel, listed_on)"""
    )
