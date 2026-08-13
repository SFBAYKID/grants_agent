"""Whether a Slack thread was opened by one of Grant's own follow-up messages.

WHY THIS EXISTS. Grant chases unfinished work, and two of those kinds —
`card_escalated` and `offer_unanswered`, the `CHANNEL_POST_KINDS` — are posted at TOP
LEVEL rather than into an existing thread, because the thing they are about has no
thread a reader could reply into. That top-level post becomes a brand-new thread root
with no `posts` row and no `slack_conversation_threads` row, and `on_message` requires
one or the other. So a rep answering Grant's own question was discarded at the gate,
BEFORE `claim_slack_event` — which is why it left no receipt, no log line and no error:
every recording mechanism sits downstream of the return.

Measured on production 2026-08-12: a rep was asked "Want me to find a contact?" about a
$500,000 award at 11:45 and answered "Yes get me a lead plz I'll call tomorrow" at
15:50. Nothing happened, and nothing could have. The nudge invites a plain threaded
reply, and a plain threaded reply was the one thing that could not reach Grant —
@-mentioning it worked the whole time, through a different handler.

This module answers only the narrow question `on_message` needs, against the ledger of
what Grant actually delivered. It is deliberately NOT a general "did Grant post this"
check: a nudge Slack rejected, or one still reserved, never reached anybody, so a
thread cannot descend from it.
"""

from __future__ import annotations

import sqlite3


def is_nudge_thread(conn: sqlite3.Connection, audience: str, thread_ts: str) -> bool:
    """Whether this thread root is a follow-up Grant itself delivered.

    Matches on `slack_ts` — the ts of the message Grant POSTED — not `anchor_ts`, which
    is the ts of the work the follow-up is ABOUT. For a top-level escalation those are
    two different threads, and the rep replies under the one they can see.

    Never raises: this runs on the inbound path of a live conversation, and a ledger
    that cannot be read must cost at most the old behaviour, never the whole event.
    """
    if not audience or not thread_ts:
        return False
    try:
        row = conn.execute(
            """SELECT 1 FROM followup_nudges
                WHERE audience=? AND slack_ts=? AND state='delivered'
                LIMIT 1""",
            (audience, thread_ts),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None
