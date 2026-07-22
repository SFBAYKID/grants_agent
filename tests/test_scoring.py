"""Grading rules — including the failure modes surfaced by the 2026-07-13 live run
(negative de-obligation amounts, expired spend windows)."""

from __future__ import annotations

from datetime import date

from grant_watch.models import FundingEventType, LeadGrade, RawItem
from grant_watch.scoring import feedback_multiplier, grade, is_fresh

TODAY = date(2026, 7, 13)  # frozen so tests never rot


def _award(**kw: object) -> RawItem:
    """Build a valid award while allowing one-field scoring overrides."""
    base = dict(
        source="usaspending:16.071",
        item_id="X1",
        title="SVPP award",
        entity="Castle Rock SD",
        state="WA",
        program="SVPP",
        amount=500_000.0,
        start="2025-10-01",
        end="2028-09-30",
        url="",
        raw={},
    )
    base.update(kw)
    return RawItem(**base)  # type: ignore[arg-type]


def test_open_window_award_is_gold() -> None:
    """A DATED, recent, open-window award is gold."""
    assert grade(_award(event_date="2026-06-01"), TODAY).grade is LeadGrade.GOLD


def test_award_freshness_splits_gold_fresh_from_silver_older() -> None:
    """Gold-fresh / silver-older split by the verified award (obligation) date: a recent
    security award is GOLD, the same award obligated over a year ago (window still open)
    is SILVER, and a closed window is WATCH."""
    assert grade(_award(event_date="2026-06-01"), TODAY).grade is LeadGrade.GOLD
    assert grade(_award(event_date="2024-01-01"), TODAY).grade is LeadGrade.SILVER
    assert (
        grade(_award(event_date="2024-01-01", end="2025-01-01"), TODAY).grade
        is LeadGrade.WATCH
    )
    # An UNKNOWN award date is SILVER, never GOLD (Chase, 2026-07-22). GOLD means "just
    # got funding"; granting it on the ABSENCE of a date grades on absent evidence and
    # asserts a recency the source cannot support. This governs the 347 undated
    # ca-grants-award rows, which stay searchable and exportable — just not proactive.
    assert grade(_award(event_date=""), TODAY).grade is LeadGrade.SILVER


def test_negative_amount_deobligation_is_watch() -> None:
    # Live run 2026-07-13 surfaced real $-7,017 rows — these are money LEAVING.
    """Verify negative amount deobligation is watch."""
    assert grade(_award(amount=-7017.0), TODAY).grade is LeadGrade.WATCH


def test_expired_spend_window_is_watch() -> None:
    """Verify expired spend window is watch."""
    assert grade(_award(end="2019-08-31"), TODAY).grade is LeadGrade.WATCH


def test_unknown_amount_or_window_is_watch() -> None:
    """Missing money/window cannot support a GOLD money-available claim."""
    assert grade(_award(amount=None), TODAY).grade is LeadGrade.WATCH
    assert grade(_award(end=""), TODAY).grade is LeadGrade.WATCH


def test_seed_source_counts_as_award() -> None:
    """A seed row is graded on the award ladder, not dropped — dated recent means GOLD."""
    assert (
        grade(_award(source="seed:svpp_csv", event_date="2026-06-01"), TODAY).grade
        is LeadGrade.GOLD
    )


def test_rfp_sources_are_silver() -> None:
    """Verify rfp sources are silver."""
    rfp = _award(source="sam.gov", program="RFP:sam.gov", amount=None, end="2026-07-22")
    assert grade(rfp, TODAY).grade is LeadGrade.SILVER
    assert (
        grade(_award(source="webs", amount=None, end=""), TODAY).grade
        is LeadGrade.WATCH
    )


def test_grants_gov_signal_is_watch_not_gold() -> None:
    # Opportunities are pipeline signals, not money in hand.
    """Verify grants gov signal is watch not gold."""
    opp = _award(source="grants.gov", amount=None, end="2026-08-04")
    assert grade(opp, TODAY).grade is LeadGrade.WATCH


def test_freshness_window() -> None:
    """Verify freshness window."""
    assert (
        is_fresh(
            _award(
                event_type=FundingEventType.AWARD_ANNOUNCED,
                event_date="2026-06-01",
            ),
            TODAY,
        )
        is True
    )
    assert is_fresh(_award(event_date="2022-10-01"), TODAY) is False
    # Spend start alone is not an award announcement date.
    assert is_fresh(_award(start="2026-06-01", event_date=""), TODAY) is False


def test_feedback_is_neutral_until_minimum_sample() -> None:
    """A handful of clicks cannot destabilize the quality rank."""
    assert feedback_multiplier([8] * 9) == 1.0
    assert feedback_multiplier([8] * 10) > 1.0
    assert feedback_multiplier([-8] * 10) < 1.0
