"""CLI safety tests for non-mutating dry runs, locks, and honest failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch import cli
from grant_watch.models import FundingEventType, RawItem


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
        event_type=FundingEventType.RECORD_OBSERVED,
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
    monkeypatch.delenv("GRANT_RICH_CARD_ENABLED", raising=False)

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


def test_rich_flag_dispatches_only_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF preserves legacy dispatch; explicit ON selects the isolated rich path."""
    from grant_watch.campaign import delivery
    from grant_watch.slack import drip

    sentinel = _readonly_only(monkeypatch)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")
    monkeypatch.setenv("GRANT_RICH_CARD_ENABLED", "1")
    monkeypatch.setattr(
        drip,
        "run_drip",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rich flag called legacy drip")
        ),
    )

    def rich_run(client: object, channel: str, conn: object, **kwargs: object) -> str:
        """Return one deterministic fake rich-delivery preview."""
        assert client is None and channel == "CGRANTS" and conn is sentinel
        assert kwargs["dry_run"] is True and kwargs["force"] is True
        return "[dry-run] rich safe"

    monkeypatch.setattr(delivery, "run", rich_run)
    assert cli.cmd_drip(force=True, dry_run=True) == 0


def test_rich_flag_off_never_enters_the_rich_delivery_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature-off equivalence: with the flag unset, cmd_drip uses the legacy drip and
    never touches rich delivery (so evaluate/freeze/render are unreachable)."""
    from grant_watch.campaign import delivery
    from grant_watch.slack import drip

    sentinel = _readonly_only(monkeypatch)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")
    monkeypatch.delenv("GRANT_RICH_CARD_ENABLED", raising=False)
    monkeypatch.setattr(
        delivery,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rich delivery entered while the flag was OFF")
        ),
    )
    monkeypatch.setattr(
        drip,
        "run_drip",
        lambda client, channel, conn, **_kwargs: (
            "[dry-run] legacy"
            if conn is sentinel and channel == "CGRANTS"
            else "unexpected"
        ),
    )
    assert cli.cmd_drip(force=True, dry_run=True) == 0


def test_rich_eligibility_miss_falls_back_to_the_daily_card(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Chase 2026-08-05: when no rich card qualifies, the restyled daily card posts —
    the tick's outcome (and exit status) comes from the path that actually ran."""
    from grant_watch.campaign import delivery
    from grant_watch.slack import drip

    sentinel = _readonly_only(monkeypatch)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")
    monkeypatch.setenv("GRANT_RICH_CARD_ENABLED", "1")
    monkeypatch.setattr(
        delivery,
        "run",
        lambda *_a, **_k: "skip: no rich award card satisfies every evidence rule",
    )
    calls: list[object] = []

    def legacy(client: object, channel: str, conn: object, **_kwargs: object) -> str:
        """Record the fallback invocation and return a normal daily outcome."""
        calls.append(conn)
        assert client is None and channel == "CGRANTS" and conn is sentinel
        return "[dry-run] would post nugget (award-brief): ..."

    monkeypatch.setattr(drip, "run_drip", legacy)
    assert cli.cmd_drip(force=True, dry_run=True) == 0
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "drip[rich]: skip: no rich award card satisfies every evidence rule" in out
    assert "falling back to the daily card" in out


def test_rich_cap_guard_and_ambiguous_outcomes_never_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spent cap, a waiting slot, or an ambiguous send must never trigger a second
    posting attempt — and a failing rich outcome must keep its non-zero exit."""
    from grant_watch.campaign import delivery
    from grant_watch.slack import drip

    _readonly_only(monkeypatch)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")
    monkeypatch.setenv("GRANT_RICH_CARD_ENABLED", "1")
    monkeypatch.setattr(
        drip,
        "run_drip",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("fell back on a non-fallback outcome")
        ),
    )
    for outcome, expected_exit in (
        ("skip: daily cap reached (1)", 0),
        ("skip: waiting for today's 10:07 Pacific slot", 0),
        ("posted rich_award for lead #7: Bartlett ISD", 0),
        ("unknown: Slack delivery could not be confirmed; no retry", 1),
        ("quarantined: Slack rejected this rich card (invalid_blocks)", 1),
    ):
        monkeypatch.setattr(delivery, "run", lambda *_a, _o=outcome, **_k: _o)
        assert cli.cmd_drip(force=True, dry_run=True) == expected_exit, outcome


def test_fallback_daily_failure_keeps_its_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambiguous send on the FALLBACK path is just as loud as on the legacy path."""
    from grant_watch.campaign import delivery
    from grant_watch.slack import drip

    _readonly_only(monkeypatch)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")
    monkeypatch.setenv("GRANT_RICH_CARD_ENABLED", "1")
    monkeypatch.setattr(
        delivery,
        "run",
        lambda *_a, **_k: "skip: no rich award card satisfies every evidence rule",
    )
    monkeypatch.setattr(
        drip,
        "run_drip",
        lambda *_a, **_k: "unknown: Slack delivery could not be confirmed",
    )
    assert cli.cmd_drip(force=True, dry_run=True) == 1


def test_rich_prepare_defaults_to_no_http_readonly_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preparation CLI requires --execute before reads/writes beyond SQLite."""
    from grant_watch.campaign import prepare_worker

    sentinel = _readonly_only(monkeypatch)
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CGRANTS")

    def preview(
        conn: object, audience: str, **kwargs: object
    ) -> prepare_worker.PreparationSummary:
        """Return one deterministic preparation summary without I/O."""
        assert conn is sentinel and audience == "CGRANTS"
        assert kwargs["dry_run"] is True
        return prepare_worker.PreparationSummary(2, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(prepare_worker, "run", preview)
    assert cli.cmd_rich_prepare(2, False, False) == 0


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


def test_running_the_cli_in_a_test_does_not_load_the_real_dotenv() -> None:
    """The test above is the one that used to poison every test after it.

    `cli.main` loads the repo's `.env` from the calling file's directory upward, so
    working directory and `tmp_path` cannot isolate it; only the conftest guard can.
    This reads the real file's KEY NAMES (never values, never printed) to know what a
    leak would look like, and asserts none of them arrived. Skipped, not passed,
    where there is no `.env` to leak — a scrubbed CI copy proves nothing here.
    """
    import os

    from dotenv import dotenv_values

    keys = set(dotenv_values(Path(cli.__file__).resolve().parents[1] / ".env"))
    if not keys:
        pytest.skip("no repo .env to leak, so nothing to prove")
    absent_before = keys - set(os.environ)
    assert absent_before, "the environment already holds every .env key"
    with pytest.raises(SystemExit):
        cli.main(["digest"])
    leaked = absent_before & set(os.environ)
    assert not leaked, f"cli.main loaded {len(leaked)} .env key(s) into the session"


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
    # THE PLAIN DRIP PATH, SAID OUT LOUD. `cmd_drip` runs the rich card first when
    # `GRANT_RICH_CARD_ENABLED` is set, and that path calls `.execute` on the
    # sentinel before the patched `run_drip` is ever reached — the droplet failure
    # of 2026-08-26 → 2026-09-04, once the CLI's own `load_dotenv()` had leaked
    # production's flag into the session. The guard in conftest stops the leak; this
    # line makes the test true in a shell that exports the flag on purpose.
    monkeypatch.delenv("GRANT_RICH_CARD_ENABLED", raising=False)
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


def _nces_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Create three Gold leads lacking nces_id: two in AR, one in MI."""
    from grant_watch import db

    conn = db.connect(tmp_path / "nces.db")
    conn.executemany(
        """INSERT INTO leads(id,source,source_item_id,lead_grade,entity_name,state,
                             status,canonical_entity_key)
           VALUES (?,?,?,?,?,?,'new',?)""",
        [
            (
                1,
                "usaspending:16.071",
                "a",
                "gold",
                "Alpha School District",
                "AR",
                "a|AR",
            ),
            (
                2,
                "usaspending:16.071",
                "b",
                "gold",
                "Beta School District",
                "AR",
                "b|AR",
            ),
            (
                3,
                "usaspending:16.071",
                "c",
                "gold",
                "Gamma School District",
                "MI",
                "c|MI",
            ),
        ],
    )
    conn.commit()
    monkeypatch.setattr(cli.db, "connect_readonly", lambda: conn)
    monkeypatch.setattr(cli.db, "connect", lambda: conn)
    return conn


def test_nces_bind_preview_writes_nothing_and_touches_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --execute the binder may not fetch NCES or write an identity."""
    from grant_watch.enrich import nces

    conn = _nces_conn(tmp_path, monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        """Provide test-local behavior for a forbidden network call."""
        raise AssertionError("preview reached the NCES API")

    monkeypatch.setattr(nces, "enrich_state_leads", fail)
    before = conn.total_changes  # type: ignore[attr-defined]
    assert cli.cmd_nces_bind(5, True) == 0
    assert conn.total_changes == before  # type: ignore[attr-defined]


def test_nces_bind_visits_the_most_pending_state_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded run spends its state budget where the most Gold leads are waiting."""
    from grant_watch.enrich import nces

    _nces_conn(tmp_path, monkeypatch)
    visited: list[str] = []

    def record(_conn: object, state: str) -> nces.EnrichmentSummary:
        """Record the visited state without contacting NCES."""
        visited.append(state)
        return nces.EnrichmentSummary(2, 2, 0)

    monkeypatch.setattr(nces, "enrich_state_leads", record)
    assert cli.cmd_nces_bind(1, False) == 0
    assert visited == ["AR"], "AR has two pending leads to MI's one"


def test_nces_bind_reports_a_failed_state_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreachable state is counted and surfaced, never silently swallowed."""
    from grant_watch.enrich import nces

    _nces_conn(tmp_path, monkeypatch)
    visited: list[str] = []

    def flaky(_conn: object, state: str) -> nces.EnrichmentSummary:
        """Fail the first state and succeed on the second."""
        visited.append(state)
        if state == "AR":
            raise ValueError("NCES pagination repeated a page without advancing")
        return nces.EnrichmentSummary(1, 1, 0)

    monkeypatch.setattr(nces, "enrich_state_leads", flaky)
    assert cli.cmd_nces_bind(5, False) == 1, "a failed state must exit non-zero"
    assert visited == ["AR", "MI"], "a bad state cannot abort the remaining budget"
