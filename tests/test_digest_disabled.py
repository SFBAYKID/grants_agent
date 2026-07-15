"""Regression tests proving multi-lead digest posting is globally unavailable."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch.slack import grant


def test_slack_digest_request_is_denied_without_database_access(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The old slash-command text returns a fixed denial and cannot query leads."""
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        """Fail if the removed command reaches any database-backed lead path."""
        raise AssertionError("digest denial must not access the database")

    monkeypatch.setattr(grant.db, "connect", fail_connect)
    assert grant._answer("digest") == grant.DIGEST_DISABLED_TEXT
    assert "disabled in every channel" in grant.DIGEST_DISABLED_TEXT


def test_digest_poster_module_is_absent() -> None:
    """The deployable package contains no module capable of formatting/posting one."""
    module_path = Path(grant.__file__).with_name("digest.py")
    assert not module_path.exists()


def test_help_does_not_advertise_digest_command() -> None:
    """Users are no longer offered a removed command."""
    assert "/grant digest" not in grant.HELP_TEXT
