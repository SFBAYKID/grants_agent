"""Two wordings per follow-up, and an honest measurement of which one gets answered.

WHY THIS IS A MEASUREMENT AND NOT AN OPTIMISER. The ask was "if a user does not
respond, maybe I should write it differently next time." Agreed — but a system that
rewrites before it can measure is guessing with extra steps, and it would be
impossible to tell whether it had helped. So this ships the smallest thing that makes
the question answerable: every proactive message records WHICH wording it used, and a
human reply in that thread marks it answered.

Selection is deliberately dull. While a wording has fewer than MIN_SAMPLE sends, the
less-used one wins, so both accumulate evidence. After that the better reply rate
wins, except one send in EXPLORE_EVERY which goes to the other so a wording that
happened to start badly is never locked out. No model, no scoring function, nothing
that can drift — the interesting judgement is in the WORDINGS, which are written by a
person and reviewed.

WHAT "ENGAGED" HONESTLY MEANS: a human posted in the thread after the nudge landed.
It does NOT mean the rep did the work — they may have phoned the district from the
car. Everything here is therefore a REPLY rate and must never be reported as an
outcome rate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# How many sends a wording needs before its rate is worth comparing. Small, because
# the volume here is one or two messages a day and waiting for statistical certainty
# would mean never choosing at all. This buys "obviously worse" detection, not
# significance, and the docstring above says so.
MIN_SAMPLE = 8
# One in five sends goes to the currently-losing wording, so a bad first run cannot
# permanently retire a wording that is actually fine.
EXPLORE_EVERY = 5


@dataclass(frozen=True)
class VariantStats:
    """How one wording has performed."""

    variant: str
    sent: int
    engaged: int

    @property
    def reply_rate(self) -> float:
        """Replies per send, or 0.0 before anything has been sent."""
        return self.engaged / self.sent if self.sent else 0.0


def stats(conn: sqlite3.Connection, subject_kind: str) -> list[VariantStats]:
    """Reply rate per wording for one subject kind, most-sent first."""
    rows = conn.execute(
        """SELECT COALESCE(variant,'a') AS variant,
                  COUNT(*) AS sent,
                  SUM(CASE WHEN engaged_at IS NOT NULL THEN 1 ELSE 0 END) AS engaged
             FROM followup_nudges
            WHERE subject_kind=? AND state='delivered'
            GROUP BY COALESCE(variant,'a')
            ORDER BY sent DESC""",
        (subject_kind,),
    ).fetchall()
    return [
        VariantStats(str(row["variant"]), int(row["sent"]), int(row["engaged"] or 0))
        for row in rows
    ]


def choose(
    conn: sqlite3.Connection, subject_kind: str, available: tuple[str, ...]
) -> str:
    """Pick the wording to send next, favouring evidence over novelty."""
    if not available:
        return ""
    if len(available) == 1:
        return available[0]
    seen = {item.variant: item for item in stats(conn, subject_kind)}
    counts = {name: seen[name].sent if name in seen else 0 for name in available}
    # Stage 1: everything gets a fair sample before anything is judged.
    if min(counts.values()) < MIN_SAMPLE:
        return min(available, key=lambda name: (counts[name], name))
    total = sum(counts.values())
    ranked = sorted(
        available,
        key=lambda name: (-(seen[name].reply_rate if name in seen else 0.0), name),
    )
    # Stage 2: mostly the winner, but keep testing the other.
    if total % EXPLORE_EVERY == 0:
        return ranked[-1]
    return ranked[0]


def mark_engagement(conn: sqlite3.Connection) -> int:
    """Mark delivered nudges a human replied to, and return how many were newly set.

    The signal is a `slack_event_receipts` row — an inbound human message — in the
    same channel and thread, timestamped after the nudge was delivered. Receipts only
    exist for events Grant processed, so this UNDERCOUNTS: a reply Grant never woke
    for is invisible. Undercounting is the safe direction, because it can only make a
    wording look worse than it is, never better.

    THE REPLY THREAD IS NOT ALWAYS `anchor_ts`. A threaded follow-up is posted INTO the
    work's own thread, so a reply carries `anchor_ts`; but the two `CHANNEL_POST_KINDS`
    are posted at TOP LEVEL and a reply carries the nudge's own `slack_ts` instead.
    Matching only `anchor_ts` meant an answered escalation still read as ignored — and
    an escalation exists precisely to report that somebody has not answered, so the one
    person who DID reply was the one it would keep naming.
    """
    cur = conn.execute(
        """UPDATE followup_nudges
              SET engaged_at = (
                    SELECT MIN(r.received_at) FROM slack_event_receipts r
                     WHERE r.channel = followup_nudges.audience
                       AND r.thread_ts IN (followup_nudges.anchor_ts,
                                           followup_nudges.slack_ts)
                       AND r.slack_user IS NOT NULL
                       AND r.received_at > followup_nudges.delivered_at)
            WHERE state='delivered'
              AND engaged_at IS NULL
              AND delivered_at IS NOT NULL
              AND EXISTS (
                    SELECT 1 FROM slack_event_receipts r
                     WHERE r.channel = followup_nudges.audience
                       AND r.thread_ts IN (followup_nudges.anchor_ts,
                                           followup_nudges.slack_ts)
                       AND r.slack_user IS NOT NULL
                       AND r.received_at > followup_nudges.delivered_at)"""
    )
    conn.commit()
    return int(cur.rowcount)


def report(conn: sqlite3.Connection) -> str:
    """A human-readable reply-rate table, for deciding whether to change a wording."""
    lines: list[str] = []
    kinds = [
        str(row["subject_kind"])
        for row in conn.execute(
            "SELECT DISTINCT subject_kind FROM followup_nudges "
            "WHERE state='delivered' ORDER BY subject_kind"
        )
    ]
    if not kinds:
        lines.append(
            "No follow-ups have been delivered yet, so there is nothing to compare."
        )
    for kind in kinds:
        lines.append(kind)
        for item in stats(conn, kind):
            enough = "" if item.sent >= MIN_SAMPLE else "  (too few to judge)"
            lines.append(
                f"  {item.variant}: {item.engaged}/{item.sent} replied "
                f"({item.reply_rate:.0%}){enough}"
            )
    lines.extend(_capacity_lines(conn))
    lines.append("")
    lines.append(
        "A reply in the thread is the only signal Grant can see. A rep who phoned "
        "the district instead counts as no reply, so these are REPLY rates, not "
        "outcome rates."
    )
    return "\n".join(lines)


def _capacity_lines(conn: sqlite3.Connection) -> list[str]:
    """What the queue dropped for want of a delivery slot, per kind.

    WHY THIS IS IN THE REPORT AND NOT JUST THE TABLE. The system has a structural
    shortfall: one card produces TWO subjects (`card_unengaged` plus `card_escalated`),
    so a card every weekday is ten subjects a week against a budget of two sends a day
    in one channel — before a single capability ask, abandoned thread or expired
    preview. `_fair_order` shares that shortfall out fairly; it cannot remove it, and
    the tail ages out at `DROP_AFTER` and is retired with a permanent `stale` row.

    That retirement is honest and durable, but it is INVISIBLE unless somebody writes
    the query. Silent capacity loss reads exactly like "there was nothing to send",
    which is the one conclusion it must never support. Counting it here means the next
    person to ask "is this thing working?" sees the backlog it could not reach.
    """
    rows = list(
        conn.execute(
            """SELECT subject_kind, COUNT(*) AS n FROM followup_nudges
                WHERE state='suppressed' AND suppress_reason='stale'
                GROUP BY subject_kind ORDER BY n DESC"""
        )
    )
    if not rows:
        return []
    total = sum(int(row["n"]) for row in rows)
    lines = [
        "",
        f"aged out unsent ({total}) — the queue could not reach these in time:",
    ]
    lines.extend(f"  {row['subject_kind']}: {row['n']}" for row in rows)
    lines.append(
        "  A large or growing number here means the daily cap is the binding "
        "constraint, not the supply of work."
    )
    return lines
