"""Email attachment encoding, ownership, delivery, and cleanup tests."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from grant_watch.notify import resend_client
from grant_watch.slack import reminder_tools
from grant_watch.spreadsheets import GeneratedArtifact, make_spreadsheet

ROSTERED_REP = "U01DPJVURHU"


def test_resend_transport_base64_encodes_a_named_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP payload follows Resend's local-file attachment contract."""
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "grant@example.test")
    sent: dict[str, object] = {}

    class _Response:
        """Successful Resend response."""

        status_code = 200
        content = b'{"id":"mail-1"}'
        text = ""

        def json(self) -> dict[str, str]:
            """Return the created email id."""
            return {"id": "mail-1"}

    class _Session:
        """Capture the exact payload sent to Resend."""

        def post(self, _url: str, **kwargs: object) -> _Response:
            """Record one request without network access."""
            sent.update(kwargs)
            return _Response()

    attachment = resend_client.EmailAttachment(
        filename="../grant-results.xlsx", content=b"workbook bytes"
    )
    outcome = resend_client.send_to_rep(
        ROSTERED_REP,
        "Results",
        "Attached.",
        attachments=[attachment],
        session=_Session(),
    )

    payload = sent["json"]
    assert isinstance(payload, dict)
    encoded = payload["attachments"][0]
    assert encoded["filename"] == "grant-results.xlsx"
    assert base64.b64decode(encoded["content"]) == b"workbook bytes"
    assert outcome.email_id == "mail-1"


@pytest.mark.parametrize(
    "attachments",
    [
        [resend_client.EmailAttachment("notes.txt", b"not a workbook")],
        [resend_client.EmailAttachment("bad\nname.xlsx", b"workbook")],
        [
            resend_client.EmailAttachment("one.xlsx", b"one"),
            resend_client.EmailAttachment("two.xlsx", b"two"),
        ],
    ],
)
def test_transport_refuses_non_workbook_or_multiple_attachments_before_http(
    attachments: list[resend_client.EmailAttachment],
) -> None:
    """The mail capability cannot be widened into arbitrary-file exfiltration."""

    class _Session:
        """HTTP must remain unreachable for an invalid artifact."""

        def post(self, *_args: object, **_kwargs: object) -> object:
            """Fail if validation occurs after the network boundary."""
            return pytest.fail("HTTP must not run")

    with pytest.raises(ValueError):
        resend_client.send_to_rep(
            ROSTERED_REP,
            "Results",
            "Attached.",
            attachments=attachments,
            session=_Session(),
        )


def test_transport_refuses_an_oversized_workbook_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The encoded request cannot cross the provider's bounded message ceiling."""
    monkeypatch.setattr(resend_client, "MAX_RAW_ATTACHMENT_BYTES", 3)

    class _Session:
        """HTTP must remain unreachable for an oversized artifact."""

        def post(self, *_args: object, **_kwargs: object) -> object:
            """Fail if the size check occurs after the network boundary."""
            return pytest.fail("HTTP must not run")

    with pytest.raises(ValueError, match="size limit"):
        resend_client.send_to_rep(
            ROSTERED_REP,
            "Results",
            "Attached.",
            attachments=[resend_client.EmailAttachment("results.xlsx", b"four")],
            session=_Session(),
        )


def test_email_results_attaches_and_cleans_the_generated_workbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requested result set reaches the inbox as Excel, then leaves no temp file."""
    _, artifact = make_spreadsheet(
        "grant-results.xlsx", [["entity"], ["Tustin Unified"]]
    )
    captured: list[resend_client.EmailAttachment] = []

    def fake_render(
        _spec: dict[str, object],
    ) -> tuple[str, GeneratedArtifact]:
        """Return a real owned workbook without querying the database."""
        return "Found one result.", artifact

    def fake_send(
        _slack_user: object,
        _subject: str,
        _body: str,
        **kwargs: object,
    ) -> object:
        """Capture the in-memory attachment while the artifact still exists."""
        assert artifact.path.exists()
        attachments = kwargs["attachments"]
        assert isinstance(attachments, list)
        captured.extend(attachments)

        class _Outcome:
            recipient = "rep@example.test"

        return _Outcome()

    monkeypatch.setattr(reminder_tools.resend_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        reminder_tools.lead_digest, "render_with_spreadsheet", fake_render
    )
    monkeypatch.setattr(reminder_tools.resend_client, "send_to_rep", fake_send)

    said = reminder_tools.email_results({"search_spec": {"state": "CA"}}, ROSTERED_REP)

    assert said == "Sent it to rep@example.test."
    assert captured[0].filename == "grant-results.xlsx"
    assert captured[0].content
    assert not artifact.path.exists()
    assert not artifact.path.parent.exists()


def test_email_results_cleans_the_workbook_when_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Resend failure cannot leak an owned temporary workbook."""
    _, artifact = make_spreadsheet("grant-results.xlsx", [["entity"], ["X"]])
    monkeypatch.setattr(reminder_tools.resend_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        reminder_tools.lead_digest,
        "render_with_spreadsheet",
        lambda _spec: ("Found one result.", artifact),
    )

    def fail(*_args: object, **_kwargs: object) -> object:
        """Simulate an ambiguous transport failure."""
        raise TimeoutError("unknown delivery")

    monkeypatch.setattr(reminder_tools.resend_client, "send_to_rep", fail)
    said = reminder_tools.email_results({}, ROSTERED_REP)

    assert said.startswith("ERROR:")
    assert not artifact.path.exists()


def test_email_results_refuses_a_missing_generated_artifact_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished workbook is reported and never degrades to an attachment-free send."""
    artifact = GeneratedArtifact(tmp_path / "missing.xlsx")
    monkeypatch.setattr(reminder_tools.resend_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        reminder_tools.lead_digest,
        "render_with_spreadsheet",
        lambda _spec: ("Found one result.", artifact),
    )
    monkeypatch.setattr(
        reminder_tools.resend_client,
        "send_to_rep",
        lambda *_args, **_kwargs: pytest.fail("HTTP must not run"),
    )

    said = reminder_tools.email_results({}, ROSTERED_REP)

    assert said.startswith("ERROR:")
