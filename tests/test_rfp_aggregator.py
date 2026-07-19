"""Aggregator RFP source — pure parsing of a bid-aggregator listing (no network).

The scraped listing is untrusted text; parse_starbridge is fuzzed on the recorded
fixture and synthetic rows. Live scraping is exercised only via a mocked _scrape.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from grant_watch import scoring
from grant_watch.enrich.finder import SourceUnreachable
from grant_watch.models import LeadGrade
from grant_watch.sources import rfp_aggregator

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "rfp" / "starbridge_physical_security.md"
).read_text()
TODAY = date(2026, 7, 19)


def _row(title: str, buyer: str, close: str, release: str = "", extra: str = "") -> str:
    """Build one synthetic Starbridge listing row block."""
    return (
        f"[**{title}**](https://starbridge.ai/rfp/{title.lower().replace(' ', '-')}) "
        f"Available\n\n[{buyer}](https://starbridge.ai/buyer/x)\n\n{extra}\n\n"
        f"Posted Date\n\nRelease: {release}\n\nDue Date\n\nClose: {close}\n\n"
        "[View Details →](https://starbridge.ai/rfp/x)\n\n"
    )


# ------------------------------------------------------------------ real fixture
def test_real_starbridge_fixture_yields_target_state_open_rfps() -> None:
    """The live listing yields the cherry-picked PA + CA open RFPs, deduped."""
    items = rfp_aggregator.parse_starbridge(_FIXTURE, TODAY)
    states = {i.state for i in items}
    assert states == {"PA", "CA"}  # only target states; NJ/IL/NY rows excluded
    assert all(i.source == "rfp" and i.program == "RFP:security" for i in items)
    assert all(i.amount is None for i in items)  # solicitation, no fabricated dollars
    # the same PA RFP listed twice (case-different slug) collapses to one lead
    assert len({i.item_id for i in items}) == len(items)
    ca = next(i for i in items if i.state == "CA")
    assert scoring.grade(ca, today=TODAY).grade is LeadGrade.GOLD  # posted 2026-06-24


# ------------------------------------------------------------------ state cherry-pick
@pytest.mark.parametrize(
    "buyer, extra, expected",
    [
        ("City of Joliet", "The City of Joliet, Illinois is soliciting", ""),  # IL, out
        ("City of Cape May", "turnkey installation", ""),  # no state named -> skip
        ("California DGS", "State of California facilities", "CA"),
        ("Tacoma Schools", "in Washington state", "WA"),
        ("Metro Agency", "Washington, D.C. headquarters", ""),  # DC, not WA
    ],
)
def test_state_cherry_pick_only_accepts_named_target_states(
    buyer: str, extra: str, expected: str
) -> None:
    """Only a clearly-named target state is accepted; DC is not Washington."""
    row = _row("Security Camera System", buyer, "Aug 25, 2026", "Jul 1, 2026", extra)
    items = rfp_aggregator.parse_starbridge(row, TODAY)
    assert [i.state for i in items] == ([expected] if expected else [])


# ------------------------------------------------------------------ openness / grading
def test_past_due_row_is_dropped() -> None:
    """A row whose Close date has passed is not surfaced."""
    row = _row("Access Control", "California DGS", "Jan 1, 2026", "Dec 1, 2025",
               "State of California")
    assert rfp_aggregator.parse_starbridge(row, TODAY) == []


def test_fresh_release_grades_gold_old_release_silver() -> None:
    """Posting date drives GOLD (recent) vs SILVER (old-but-open)."""
    fresh = _row("Camera RFP", "California DGS", "Sep 1, 2026", "Jul 10, 2026",
                 "State of California")
    stale = _row("Camera RFP 2", "California DGS", "Sep 1, 2026", "Jan 1, 2026",
                 "State of California")
    g = rfp_aggregator.parse_starbridge(fresh, TODAY)[0]
    s = rfp_aggregator.parse_starbridge(stale, TODAY)[0]
    assert scoring.grade(g, today=TODAY).grade is LeadGrade.GOLD
    assert scoring.grade(s, today=TODAY).grade is LeadGrade.SILVER


def test_unavailable_status_row_is_dropped() -> None:
    """A closed ('Unavailable') listing row is never surfaced."""
    row = (
        "[**Camera RFP**](https://starbridge.ai/rfp/camera-rfp) Unavailable\n\n"
        "[California DGS](https://starbridge.ai/buyer/x)\n\nState of California\n\n"
        "Close: Sep 1, 2026\n\n"
    )
    assert rfp_aggregator.parse_starbridge(row, TODAY) == []


def test_non_security_row_is_dropped() -> None:
    """A target-state row that is not physical security is excluded."""
    row = _row("Janitorial Services", "California DGS", "Sep 1, 2026", "Jul 1, 2026",
               "State of California cleaning contract")
    assert rfp_aggregator.parse_starbridge(row, TODAY) == []


# ------------------------------------------------------------------ poll I/O
def test_poll_raises_when_listing_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked/thin scrape is 'could not look', never a false 'no RFPs'."""
    monkeypatch.setattr(rfp_aggregator, "_scrape", lambda url: "")
    with pytest.raises(SourceUnreachable):
        rfp_aggregator.poll()


def test_poll_parses_a_mocked_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """poll() scrapes and parses; a good listing yields target-state items."""
    monkeypatch.setattr(rfp_aggregator, "_scrape", lambda url: _FIXTURE)
    items = rfp_aggregator.poll()
    assert items and {i.state for i in items} <= set(rfp_aggregator.TARGET_STATES)
