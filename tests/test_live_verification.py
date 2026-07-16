"""Offline safety and parser tests for the opt-in permanent live verifier."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grant_watch import live_verification as live
from grant_watch.models import RawItem


def _award(**changes: object) -> RawItem:
    """Build the golden parsed award while allowing one-field drift tests."""
    values: dict[str, object] = {
        "source": "usaspending:16.071",
        "item_id": live.TARGET_AWARD_ID,
        "title": "School Violence Prevention Program",
        "entity": live.TARGET_ENTITY,
        "state": live.TARGET_STATE,
        "program": "SVPP",
        "amount": live.TARGET_AMOUNT,
        "start": live.TARGET_START,
        "end": live.TARGET_END,
        "url": ("https://www.usaspending.gov/award/ASST_NON_15JCOPS25GG01291SSIX_015"),
        "raw": {},
    }
    values.update(changes)
    return RawItem(**values)  # type: ignore[arg-type]  # typed drift fixture


def test_exact_award_requires_every_golden_field() -> None:
    """A matching ID cannot hide changed recipient, amount, or spend-window data."""
    evidence = live._find_exact_award([_award()])
    assert evidence.amount == 500_000
    with pytest.raises(RuntimeError, match="award fields drifted"):
        live._find_exact_award([_award(amount=499_999)])


def test_contact_name_and_title_must_share_one_directory_record() -> None:
    """Page-wide name/title co-occurrence is insufficient evidence of association."""
    valid = """
    <div class="fsConstituentItem">
      <h3 class="fsFullName">Vic Chalabian</h3>
      <div class="fsTitles"><strong>Titles:</strong> IT Systems Manager</div>
    </div>
    """
    evidence = live._find_contact_record(valid)
    assert evidence.association == "same official directory record"

    unrelated = """
    <div class="fsConstituentItem"><h3 class="fsFullName">Vic Chalabian</h3></div>
    <div class="fsConstituentItem"><div class="fsTitles">IT Systems Manager</div></div>
    """
    with pytest.raises(RuntimeError, match="same-record"):
        live._find_contact_record(unrelated)


def test_contact_redirect_must_remain_on_exact_official_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect to a lookalike host is rejected before its content is trusted."""
    monkeypatch.setattr(
        live,
        "polite_get",
        lambda _url: SimpleNamespace(
            url="https://bcchs.example.net/staff",
            text="",
        ),
    )
    with pytest.raises(RuntimeError, match="non-allowlisted"):
        live.verify_contact_live()


def test_live_gate_requires_flag_env_and_non_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default pytest and CI execution can never make these live requests."""
    monkeypatch.delenv("GRANT_LIVE_VERIFICATION", raising=False)
    monkeypatch.delenv("CI", raising=False)
    assert live._live_execution_allowed(True) is False
    monkeypatch.setenv("GRANT_LIVE_VERIFICATION", "1")
    assert live._live_execution_allowed(False) is False
    assert live._live_execution_allowed(True) is True
    monkeypatch.setenv("CI", "1")
    assert live._live_execution_allowed(True) is False
