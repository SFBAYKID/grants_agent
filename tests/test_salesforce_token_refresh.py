"""A guessed token expiry must not become a silent outage.

Salesforce reports no `expires_in` for client_credentials, so `_auth` guesses 25
minutes. When the connected app's real session is shorter, the reader hands out a
dead token and every lookup fails until the guess rolls over -- observed live on
2026-08-22, where one lookup succeeded and the retry 3m28s later did not.
"""

from __future__ import annotations

from typing import Any

import pytest

from grant_watch.enrich import salesforce


class _Response:
    """Minimal requests.Response stand-in with a real status code."""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        """Record the status and body this fake response will report."""
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        """Raise the way requests does, so the caller path is exercised."""
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        """Return the decoded body."""
        return self._payload


def test_a_dead_token_is_refreshed_and_the_read_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE fix: a rejected session must recover, not surface as a tool error."""
    calls: list[str] = []
    refreshed: list[bool] = []

    def fake_get(url: str, **kwargs: Any) -> _Response:
        """Reject the first bearer once, then accept the refreshed one."""
        bearer = kwargs["headers"]["Authorization"]
        calls.append(bearer)
        if bearer == "Bearer dead":
            return _Response(401)
        return _Response(200, {"records": [{"Id": "001"}]})

    def fake_auth(force: bool = False) -> tuple[str, str]:
        """Only a forced refresh yields a live token."""
        refreshed.append(force)
        return ("live" if force else "dead"), "https://x.my.salesforce.com"

    monkeypatch.setattr(salesforce.requests, "get", fake_get)
    monkeypatch.setattr(salesforce, "_auth", fake_auth)

    body = salesforce._readonly_get(
        "query", {"q": "SELECT Id FROM Account"}, "dead", "https://x.my.salesforce.com"
    )

    assert body == {"records": [{"Id": "001"}]}
    assert calls == ["Bearer dead", "Bearer live"], "the read must be retried once"
    assert refreshed == [True], "the retry must FORCE a refresh, not reuse the cache"


def test_a_persistent_rejection_still_fails_and_does_not_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. A retry that never gives up would hang the bot on a bad key."""
    calls: list[str] = []

    def always_401(url: str, **kwargs: Any) -> _Response:
        """Reject every bearer, however fresh."""
        calls.append(kwargs["headers"]["Authorization"])
        return _Response(401)

    monkeypatch.setattr(salesforce.requests, "get", always_401)
    monkeypatch.setattr(
        salesforce, "_auth", lambda force=False: ("still-dead", "https://x")
    )

    with pytest.raises(RuntimeError, match="HTTP 401"):
        salesforce._readonly_get("query", {"q": "x"}, "dead", "https://x")
    assert len(calls) == 2, "exactly one retry -- never a loop"


def test_a_non_auth_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 is Salesforce being unwell; hammering it again is not the fix."""
    calls: list[str] = []

    def server_error(url: str, **kwargs: Any) -> _Response:
        """Fail with a server error rather than an auth error."""
        calls.append(url)
        return _Response(500)

    monkeypatch.setattr(salesforce.requests, "get", server_error)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        salesforce._readonly_get("query", {"q": "x"}, "tok", "https://x")
    assert len(calls) == 1, "only a rejected SESSION justifies a second call"
