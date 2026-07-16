"""Offline Salesforce configuration-error regression tests."""

from __future__ import annotations

import pytest

from grant_watch.enrich import salesforce


def test_missing_reader_configuration_is_reported_without_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack receives an actionable unavailable state when reader secrets are absent."""
    for key in (
        "SALESFORCE_MY_DOMAIN_URL",
        "SALESFORCE_CLIENT_ID",
        "SALESFORCE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    result = salesforce.lookup("Test District", state="CA")
    assert result.status is salesforce.SFResultStatus.UNAVAILABLE
    assert result.error.startswith("Salesforce reader is not configured")
    assert "KeyError" not in result.error
