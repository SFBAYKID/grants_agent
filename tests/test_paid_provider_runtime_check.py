"""Read-only paid-provider deployment preflight command."""

from __future__ import annotations

import pytest

from grant_watch import paid_provider_runtime_check as runtime_check


def test_runtime_check_reports_all_fail_closed_issues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken authority exits nonzero without hiding a second issue."""
    monkeypatch.setattr(runtime_check, "load_dotenv", lambda: False)
    monkeypatch.setattr(
        runtime_check,
        "runtime_configuration_issues",
        lambda: ["authority missing", "ledger mismatched"],
    )
    assert runtime_check.main() == 2
    output = capsys.readouterr().out
    assert "refused: authority missing" in output
    assert "refused: ledger mismatched" in output


def test_runtime_check_accepts_a_complete_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A valid read-only preflight gives one unambiguous success line."""
    monkeypatch.setattr(runtime_check, "load_dotenv", lambda: False)
    monkeypatch.setattr(runtime_check, "runtime_configuration_issues", lambda: [])
    assert runtime_check.main() == 0
    assert "verified" in capsys.readouterr().out
