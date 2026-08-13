"""The declared state vocabularies actually reject an unknown state.

Both `NUDGE_STATES` and `ATTEMPT_STATES` carry comments promising that a new state is
"a code change with a failing test, not a runtime IntegrityError" — but neither tuple
was referenced anywhere, so any string reached the database. These tests are the
failing test that comment describes, and the amount-label guard is the same shape: a
constant that stated a rule nothing enforced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.campaign import policy
from grant_watch.migrations_campaign_attempts import ATTEMPT_STATES
from grant_watch.migrations_nudges import NUDGE_STATES
from grant_watch.slack import nudges
from grant_watch.slack.nudge_sources import NudgeCandidate

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


def _candidate(subject_id: str = "sub-1") -> NudgeCandidate:
    """One minimal candidate; only the state argument is under test.

    `due_at`, `drop_after` and `priority_at` are derived properties, not fields — they
    are computed from `stalled_at`, so only that is supplied here.
    """
    return NudgeCandidate(
        subject_kind="crm_preview_expired",
        subject_id=subject_id,
        audience="C0TEST",
        target_slack="U0REP",
        anchor_ts="1000.0001",
        stalled_at=NOW - timedelta(days=1),
        observed={},
    )


def test_recording_a_nudge_rejects_a_state_outside_the_vocabulary(
    tmp_path: Path,
) -> None:
    """A typo'd state fails loudly instead of being written to the ledger."""
    conn = db.connect(tmp_path / "states.db")
    with pytest.raises(ValueError, match="unknown nudge state"):
        nudges._record(conn, _candidate(), NOW, state="delivred", reason=None)
    assert conn.execute("SELECT COUNT(*) FROM followup_nudges").fetchone()[0] == 0
    conn.close()


def test_every_declared_nudge_state_is_still_accepted(tmp_path: Path) -> None:
    """The guard rejects only the unknown — no real state was locked out."""
    conn = db.connect(tmp_path / "states-ok.db")
    for index, state in enumerate(NUDGE_STATES):
        nudges._record(conn, _candidate(f"sub-{index}"), NOW, state=state, reason=None)
    written = {
        row[0] for row in conn.execute("SELECT DISTINCT state FROM followup_nudges")
    }
    assert written == set(NUDGE_STATES)
    conn.close()


def test_attempt_states_vocabulary_is_enforced_on_close() -> None:
    """`_close_attempt` refuses a state the migration never declared."""
    from grant_watch.enrich import salesforce_campaign_batch as batch

    with pytest.raises(ValueError, match="unknown attempt state"):
        batch._close_attempt(None, "attempt-1", state="finished")
    assert "prepared" in ATTEMPT_STATES  # the real vocabulary is unchanged


def test_an_award_amount_may_not_be_labelled_as_a_remaining_balance() -> None:
    """The money line may not claim a balance; other lines may say "available"."""
    assert policy.forbidden_amount_label("$500,000 remaining · SVPP") == "remaining"
    assert policy.forbidden_amount_label("$500,000 available") == "available"
    assert policy.forbidden_amount_label("$500,000 · SVPP") == ""
    # An honest neighbouring line is untouched: only the line carrying the figure is
    # inspected, so a true sentence elsewhere cannot fail the card closed.
    assert policy.forbidden_amount_label("$500,000 · SVPP\nContact available") == ""


def test_a_card_whose_money_line_claims_a_balance_is_refused() -> None:
    """card.render fails closed, so drip quarantines it rather than posting it.

    A hostile or mistaken program name is the realistic way this reaches a card: the
    program string is rendered on the same line as the amount, so "SVPP funds
    remaining" turns an obligated figure into a balance claim nobody measured.
    """
    from tests.test_rich_card import _snapshot

    from grant_watch.campaign import card

    with pytest.raises(ValueError, match="claims a balance"):
        card.render(_snapshot(program="SVPP funds remaining"))
    # CONTROL: the same card with an ordinary program name still renders, so the guard
    # rejects the claim rather than the program field.
    assert card.render(_snapshot(program="SVPP")).blocks
