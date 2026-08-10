"""Which WORDING a follow-up uses, and whether anyone answers it.

Split from `test_nudges.py` at the 1,000-line cap (CLAUDE.md rule 4), mirroring the
split of `nudges.py` into worker and messages. The boundary is the same one: these
tests are about the sentence and its measurement, not about whether, when or to whom.

The distinctness test is the load-bearing one. Twice in one session the A/B ledger
was comparing a sentence with itself — first for three subject kinds, then for two
more that delegated to builders taking no variant — which would have filled the
ledger with two labels carrying identical words and then picked a winner from noise.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from grant_watch import db
from grant_watch.slack import nudges

CHANNEL = "C0TEST"
REP = "U0REP"
# A Wednesday inside the business window (11:00 Pacific).
NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database."""
    return db.connect(tmp_path / "v.db")


def test_both_wordings_get_a_fair_sample_before_either_is_judged(
    tmp_path: Path,
) -> None:
    """Measurement before optimisation.

    A system that rewrites before it can measure is guessing with extra steps. While
    a wording has fewer than MIN_SAMPLE sends the less-used one wins, so both
    accumulate evidence rather than the first one taken becoming permanent.
    """
    from grant_watch.slack import nudge_variants

    conn = _conn(tmp_path)
    picks = []
    for index in range(6):
        variant = nudge_variants.choose(conn, "card_unengaged", nudges.VARIANTS)
        picks.append(variant)
        conn.execute(
            "INSERT INTO followup_nudges (id,subject_kind,subject_id,audience,"
            "target_slack,anchor_ts,policy_version,due_at,drop_after,state,"
            "observed_json,delivery_key,reserved_at,delivered_at,variant) "
            "VALUES (?,'card_unengaged',?,?,?,'1.1','v',?,?,'delivered','{}',?,?,?,?)",
            (
                f"n{index}",
                str(index),
                CHANNEL,
                REP,
                NOW.isoformat(),
                NOW.isoformat(),
                f"k{index}",
                NOW.isoformat(),
                NOW.isoformat(),
                variant,
            ),
        )
        conn.commit()
    assert picks.count("a") == 3 and picks.count("b") == 3, (
        f"one wording was starved of evidence: {picks}"
    )
    conn.close()


def test_a_reply_in_the_thread_marks_the_wording_answered(tmp_path: Path) -> None:
    """The only engagement signal Grant can honestly see is a reply it received.

    It UNDERCOUNTS — a reply Grant never woke for leaves no receipt — and that is the
    safe direction, because it can only make a wording look worse than it is.
    """
    from grant_watch.slack import nudge_variants

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO followup_nudges (id,subject_kind,subject_id,audience,"
        "target_slack,anchor_ts,policy_version,due_at,drop_after,state,"
        "observed_json,delivery_key,reserved_at,delivered_at,variant) "
        "VALUES ('n1','card_unengaged','1',?,?,'700.1','v',?,?,'delivered','{}',"
        "'k1',?,?,'a')",
        (
            CHANNEL,
            REP,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()
    assert nudge_variants.mark_engagement(conn) == 0, "nothing replied yet"

    conn.execute(
        "INSERT INTO slack_event_receipts (event_id,workspace,channel,thread_ts,"
        "slack_user,state,received_at) VALUES ('e1','T1',?,'700.1',?,'complete',?)",
        (CHANNEL, REP, (NOW + timedelta(minutes=5)).isoformat()),
    )
    conn.commit()
    assert nudge_variants.mark_engagement(conn) == 1
    stats = {s.variant: s for s in nudge_variants.stats(conn, "card_unengaged")}
    assert stats["a"].engaged == 1 and stats["a"].reply_rate == 1.0
    conn.close()


@pytest.mark.parametrize(
    ("kind", "target", "observed"),
    [
        ("card_unengaged", "", {"entity_name": "Wilder School District #133"}),
        ("card_unengaged", REP, {"entity_name": "Wilder", "amount_usd": 450000}),
        ("crm_batch_blocked", REP, {"organizations": 3}),
        ("crm_batch_partial", REP, {}),
        ("crm_preview_expired", REP, {}),
        ("thread_abandoned", REP, {}),
        # The two the guardian caught still discarding the label on the DEPLOYED
        # bytes: both delegate to their own builder, and neither took a variant.
        (
            "capability_now_available",
            REP,
            {
                "ask_text": "Email those to kerry@monarchconnected.com",
                "capability": "email_results",
                "asked_on": "23 July",
            },
        ),
        (
            "card_escalated",
            REP,
            {
                "entity_name": "Hoxie School District",
                "amount_usd": 500000,
                "tagged_slack": "U08C1NBH875",
            },
        ),
    ],
)
def test_every_wording_pair_is_actually_two_different_sentences(
    kind: str, target: str, observed: dict[str, object]
) -> None:
    """A/B testing a sentence against ITSELF is worse than not testing at all.

    Three of the five kinds returned byte-identical text for both labels, and the
    untagged card — which is the entire live queue — was one of them. The ledger
    would have filled with rows marked `a` and `b` carrying the same sentence,
    `nudge-report` would have shown two reply rates as though they compared
    wordings, and after MIN_SAMPLE sends `choose()` would have started preferring a
    "winner" picked purely by noise. That is precisely the superstition
    nudge_variants exists to prevent, built into the thing meant to prevent it.

    Parametrised over the shapes that actually reach production, because the tagged
    and untagged card paths are different branches and only one of them had a
    second wording.
    """
    candidate = nudges.NudgeCandidate(
        subject_kind=kind,
        subject_id="1",
        audience=CHANNEL,
        target_slack=target,
        anchor_ts="1.1",
        stalled_at=NOW,
        observed=observed,
    )
    first = nudges.build_message(candidate, "a")
    second = nudges.build_message(candidate, "b")
    assert first != second, (
        f"{kind} (target={target or 'none'}) sends the same sentence for both "
        "variants, so any reply-rate comparison between them is noise"
    )
    assert first.strip() and second.strip()


def test_the_member_add_confirmation_carries_the_campaign_link() -> None:
    """Chase had to ask "give me the link" after 13 leads were added.

    The CREATE confirmation carried a link and the member-add one did not, so a
    thread that had just written 13 records to Salesforce ended with the rep going
    to hunt for them. A confirmation that reports work without a way to see it is
    half a message.
    """
    from grant_watch.enrich import salesforce_campaign_execution as execution

    class _Gateway:
        """Only the link builder matters here."""

        def lightning_link(self, sobject: str, record_id: str) -> str:
            """Build a fake but well-formed record link."""
            return f"https://writer.test/lightning/r/{sobject}/{record_id}/view"

    link = execution._campaign_link(_Gateway(), "701UZ00000uW9jBYAS")
    assert link.endswith("/lightning/r/Campaign/701UZ00000uW9jBYAS/view")

    # A gateway that cannot build one must degrade, never raise: the write already
    # succeeded and a missing link must not turn that into an error.
    class _Broken:
        """A gateway whose link builder fails."""

        def lightning_link(self, sobject: str, record_id: str) -> str:
            """Fail the way an expired token would."""
            raise RuntimeError("token expired")

    assert execution._campaign_link(_Broken(), "701UZ00000uW9jBYAS") == ""
    assert execution._campaign_link(None, "701UZ00000uW9jBYAS") == ""


def test_the_daily_slots_vary_by_day_but_not_within_one(tmp_path: Path) -> None:
    """A fixed schedule makes Grant read as the cron job it is.

    Seeded by (date, audience) so every tick of a day agrees — a per-tick roll would
    move the goalpost every 30 minutes, which is how the daily card once front-loaded
    its entire day into the first hour.
    """
    from datetime import date as _date

    monday = nudges.daily_slots(_date(2026, 8, 10), CHANNEL)
    again = nudges.daily_slots(_date(2026, 8, 10), CHANNEL)
    tuesday = nudges.daily_slots(_date(2026, 8, 11), CHANNEL)

    assert monday == again, "two ticks on the same day drew different times"
    assert monday != tuesday, "every day would land at the identical minute"
    assert len(monday) == nudges.MAX_NUDGES_PER_DAY
    assert monday[0] < monday[1], "slots are not ordered"


def test_no_slot_is_ever_drawn_past_the_last_cron_tick(tmp_path: Path) -> None:
    """A slot after the final tick means NEVER, and it fails silently.

    Every tick would log "holding for today's slot" and nothing would post — two
    lines that both read as routine. It shipped exactly that way against a cron that
    ran only at 09:15 and 14:15: any slot after 14:15 was unreachable, so more than
    half the band quietly meant silence. The cron is now every 30 minutes to 15:30,
    and the band ends at 15:00 with a spare tick.
    """
    from datetime import date as _date, time as _time

    latest = _time(0, 0)
    for day in range(1, 29):
        for channel in (CHANNEL, "C0B02721MNK", "C01DGT9D11D"):
            for slot in nudges.daily_slots(_date(2026, 9, day), channel):
                latest = max(latest, slot)
    assert latest <= _time(15, 30), (
        f"a slot at {latest} PT is past the last cron tick and can never fire"
    )
    assert nudges.NUDGE_BAND_END_PT <= _time(15, 30)


def test_two_slots_are_never_stacked_into_the_same_gap(tmp_path: Path) -> None:
    """Randomness must not defeat MIN_GAP by drawing both slots minutes apart."""
    from datetime import date as _date, datetime as _dt

    gap = nudges.MIN_GAP
    for day in range(1, 29):
        slots = nudges.daily_slots(_date(2026, 9, day), CHANNEL)
        if len(slots) < 2:
            continue
        first = _dt.combine(_date(2026, 9, day), slots[0])
        second = _dt.combine(_date(2026, 9, day), slots[1])
        assert second - first >= gap, f"day {day}: {slots} are closer than {gap}"
