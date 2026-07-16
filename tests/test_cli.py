"""CLI safety tests for non-mutating dry runs, locks, and honest failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch import cli
from grant_watch.models import RawItem


def _item() -> RawItem:
    """Return one minimal parser result for CLI orchestration tests."""
    return RawItem(
        source="test",
        item_id="1",
        title="test",
        entity="Test District",
        state="CA",
        program="",
        amount=None,
        start="",
        end="",
        url="",
        raw={},
    )


def test_poll_dry_run_never_opens_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run source checks must not create or migrate SQLite."""
    monkeypatch.setattr(cli, "_active_pollers", lambda: [("Test", lambda: [_item()])])

    def fail_connect() -> None:
        """Provide test-local behavior for fail connect."""
        raise AssertionError("dry-run opened the database")

    monkeypatch.setattr(cli.db, "connect", fail_connect)
    assert cli.cmd_poll(None, dry_run=True) == 0


def test_poll_failure_returns_nonzero_and_redacts_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial source run fails cron without exposing URL-embedded API keys."""
    secret = "do-not-print-me"
    monkeypatch.setenv("SAM_API_KEY", secret)

    def broken() -> list[RawItem]:
        """Provide test-local behavior for broken."""
        raise RuntimeError(f"https://example.test?q=1&api_key={secret}")

    monkeypatch.setattr(cli, "_active_pollers", lambda: [("Broken", broken)])
    assert cli.cmd_poll(None, dry_run=True) == 1
    stderr = capsys.readouterr().err
    assert secret not in stderr
    assert "[REDACTED]" in stderr


def test_unknown_source_filter_returns_distinct_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typoed --source cannot report a successful no-op."""
    monkeypatch.setattr(cli, "_active_pollers", lambda: [("Test", lambda: [_item()])])
    assert cli.cmd_poll("missing", dry_run=True) == 2


def _readonly_only(monkeypatch: pytest.MonkeyPatch) -> object:
    """Make writable DB access fail and return a sentinel read-only connection."""
    sentinel = object()

    def fail_connect() -> None:
        """Provide test-local behavior for fail connect."""
        raise AssertionError("dry-run opened the writable database")

    monkeypatch.setattr(cli.db, "connect", fail_connect)
    monkeypatch.setattr(cli.db, "connect_readonly", lambda: sentinel)
    return sentinel


def test_drip_dry_run_uses_readonly_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proactive preview cannot migrate or write SQLite."""
    from grant_watch.slack import drip

    sentinel = _readonly_only(monkeypatch)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")

    def fake_drip(
        client: object,
        channel: str,
        conn: object,
        force: bool = False,
        dry_run: bool = False,
    ) -> str:
        """Provide test-local behavior for fake drip."""
        assert client is None and channel == "CGRANTS" and conn is sentinel
        assert force is True and dry_run is True
        return "[dry-run] safe"

    monkeypatch.setattr(drip, "run_drip", fake_drip)
    assert cli.cmd_drip(force=True, dry_run=True) == 0


def test_outreach_retry_dry_run_uses_readonly_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outreach retry preview cannot mutate its durable outbox."""
    from grant_watch import persequor_client

    sentinel = _readonly_only(monkeypatch)

    def fake_retry(
        conn: object, dry_run: bool = False
    ) -> persequor_client.RetrySummary:
        """Provide test-local behavior for fake retry."""
        assert conn is sentinel and dry_run is True
        return persequor_client.RetrySummary(1, 0, 1, 0)

    monkeypatch.setattr(persequor_client, "retry_pending", fake_retry)
    assert cli.cmd_outreach_retry(dry_run=True) == 0


def test_salesforce_sync_dry_run_uses_readonly_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only CRM shadow checks cannot write local snapshots or migrations."""
    from grant_watch.enrich import salesforce_sync

    sentinel = _readonly_only(monkeypatch)

    def fake_sync(
        conn: object, limit: int = 25, dry_run: bool = False
    ) -> salesforce_sync.SyncSummary:
        """Provide test-local behavior for fake sync."""
        assert conn is sentinel and limit == 3 and dry_run is True
        return salesforce_sync.SyncSummary(1, 1, 0, 0, 0, 0, 0)

    monkeypatch.setattr(salesforce_sync, "sync", fake_sync)
    assert cli.cmd_salesforce_sync(limit=3, dry_run=True) == 0


@pytest.mark.parametrize("command", ["digest", "slack-smoke"])
def test_direct_slack_posting_cli_commands_do_not_exist(command: str) -> None:
    """No local or cron-capable command can directly post test or digest messages."""
    with pytest.raises(SystemExit) as exc:
        cli.main([command])
    assert exc.value.code == 2


def test_unresolved_cron_outcomes_return_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown Slack delivery and degraded CRM/outreach work cannot look healthy."""
    from grant_watch import persequor_client
    from grant_watch.enrich import salesforce_sync
    from grant_watch.slack import drip

    sentinel = object()
    monkeypatch.setattr(cli.db, "connect", lambda: sentinel)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "offline-token")
    monkeypatch.setattr(
        drip,
        "run_drip",
        lambda *_args, **_kwargs: "unknown: Slack delivery could not be confirmed",
    )
    assert cli.cmd_drip(force=True, dry_run=False) == 1

    monkeypatch.setattr(
        persequor_client,
        "retry_pending",
        lambda *_args, **_kwargs: persequor_client.RetrySummary(1, 0, 1, 0),
    )
    assert cli.cmd_outreach_retry(dry_run=False) == 1

    monkeypatch.setattr(
        salesforce_sync,
        "sync",
        lambda *_args, **_kwargs: salesforce_sync.SyncSummary(2, 1, 0, 0, 1, 0, 2),
    )
    assert cli.cmd_salesforce_sync(limit=2, dry_run=False) == 1


def test_slack_failure_cli_lists_and_reviews_without_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Operators can acknowledge a failed turn only after it is listed explicitly."""
    conn = cli.db.connect(tmp_path / "events.db")
    cli.db.claim_slack_event(conn, "evt-1", "T1", "C1", "1.0", "U1")
    cli.db.finish_slack_event(
        conn,
        "evt-1",
        error="delivery failed",
        action_state="complete",
        delivery_state="failed",
    )
    monkeypatch.setattr(cli.db, "connect_readonly", lambda: conn)
    monkeypatch.setattr(cli.db, "connect", lambda: conn)
    assert cli.cmd_slack_failures() == 1
    assert cli.cmd_slack_failures("evt-1") == 0
    assert cli.cmd_slack_failures() == 0
