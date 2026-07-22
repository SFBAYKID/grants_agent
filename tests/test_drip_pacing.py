"""Drip failure modes that silence or misdirect the product.

Split from test_drip.py, which is near the 1000-line cap (Constitution rule 4).
test_drip.py owns WHAT Grant says; this file owns the ways it can stop saying anything,
or say it to the wrong person. Every test here corresponds to a defect that was proven
against a real database before it was fixed — none is speculative.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from slack_sdk.errors import SlackApiError

from grant_watch import db, territory
from grant_watch.slack import drip

from test_drip import _mk_lead, _mk_rfp, _SlackClient


def test_ambiguous_send_does_not_wedge_the_drip_forever(tmp_path: Path) -> None:
    """C1 (architectural-critic, 2026-07-22) — reproduced against a real database.

    An ambiguous Slack send (5xx, ratelimited, socket timeout) leaves
    notification_outbox in state 'unknown' and is DELIBERATELY never retried, because
    the message may in fact have been delivered. But the lead stayed `status='new'`,
    absent from `posts`, and still the winner of `_best_nugget`'s deterministic `max()`
    over a static pool — so every later tick re-picked it, `reserve_notification`
    returned None on the existing delivery_key, and `run_drip` returned early. One
    ambiguous send silenced the entire product permanently, behind a benign-looking
    `skip:` line and exit code 0. Over ~250 posts a year that is close to certain.
    """
    conn = db.connect(tmp_path / "t.db")
    stuck = _mk_lead(conn, iid="STUCK", entity="Stuck District", amount=500_000.0,
                     start="2025-10-10", end="2028-09-30", backfill=True)
    _mk_lead(conn, iid="NEXT", entity="Next District", amount=400_000.0,
             start="2025-10-10", end="2028-09-30", backfill=True)
    failing = _SlackClient(fail=True)
    assert drip.run_drip(failing, "C1", conn, force=True).startswith("unknown:")
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert conn.execute(
        "SELECT state FROM notification_outbox"
    ).fetchone()["state"] == "unknown"
    # The ambiguous lead must never be retried...
    assert all(row["id"] != stuck for row in db.nugget_candidates(conn, "C1"))
    # ...but the queue must ADVANCE rather than stop.
    good = _SlackClient()
    outcome = drip.run_drip(good, "C1", conn, force=True)
    assert outcome.startswith("posted"), f"drip wedged after an ambiguous send: {outcome}"
    assert "Next District" in outcome
    assert good.calls == 1


def test_an_ambiguous_send_does_not_bury_the_lower_tiers(tmp_path: Path) -> None:
    """`run_drip`'s early return meant one stuck GOLD lead also hid every RFP and
    bulletin beneath it — the outage was total, not partial."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="ONLYGOLD", start="2025-10-10", end="2028-09-30", backfill=True)
    _mk_rfp(conn, iid="RFP1", end="2031-12-31")
    failing = _SlackClient(fail=True)
    assert drip.run_drip(failing, "C1", conn, force=True).startswith("unknown:")
    choice = drip.pick(conn, "C1")
    assert choice is not None, "a stuck gold lead silenced every tier"
    assert choice[0] == "rfp"


def test_inferred_state_never_tags_a_rep(tmp_path: Path) -> None:
    """C2 — `rfp_aggregator._row_state` derives state by searching the row's prose for
    five state NAMES, so 'Oregon City Schools, Ohio' reads as OR and '1600 Pennsylvania
    Avenue NW' as PA. Before territory tagging that was a wrong two-letter label; now it
    would send a rep's phone a notification asserting they own someone else's deal."""
    conn = db.connect(tmp_path / "t.db")
    _mk_rfp(conn, iid="OHIO", entity="Oregon City Schools", end="2031-12-31")
    conn.execute("UPDATE leads SET state='OR' WHERE source_item_id='OHIO'")
    conn.commit()
    client = _SlackClient()
    assert drip.run_drip(client, "C1", conn, force=True).startswith("posted")
    text = str(client.last_kwargs["text"])
    assert "<@" not in text, f"an inferred state tagged a rep: {text}"


def test_state_provenance_gate_still_allows_verified_sources() -> None:
    """The gate must block inference without silencing legitimate tagging."""
    assert territory.state_is_verified("usaspending:16.071")
    assert territory.state_is_verified("ca-grants-award:2024-2025")
    assert territory.state_is_verified("webs")
    assert not territory.state_is_verified("rfp")
    assert not territory.state_is_verified(None)  # unknown source fails safe
    assert territory.mention_line("CA", "usaspending:16.071") != ""
    assert territory.mention_line("CA", "rfp") == ""


@pytest.mark.parametrize(
    ("start", "end"),
    [("17:00", "17:30"), ("18:00", "23:00"), ("02:00", "03:00"), ("23:00", "23:30")],
)
def test_band_outside_the_delivery_window_is_clamped(
    start: str, end: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H5 — `in_window` closes at 17:00 PT, so a band of 17:00-17:30 draws a target no
    tick can ever admit. Every tick then logs `holding for today's 17:13 PT slot` and
    `outside window`, both of which read as routine, and no card is posted again. These
    variables exist to be hand-tuned, which is exactly when a typo happens."""
    monkeypatch.setenv("DRIP_SLOT_START_PT", start)
    monkeypatch.setenv("DRIP_SLOT_END_PT", end)
    slot = drip.daily_slot(date(2026, 7, 22), "C1")
    assert time(4, 0) <= slot <= time(16, 30), f"{start}-{end} drew unreachable {slot}"


# ------------------------------------------- durable quarantine + failure classification
class _RejectingClient:
    """A Slack client that ANSWERS and refuses — HTTP 200 with an error payload."""

    def __init__(self, code: str) -> None:
        """Record the Slack error code this client will refuse with."""
        self.code, self.calls = code, 0

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:  # noqa: N802
        """Raise the way slack_sdk does for a definitive rejection."""
        self.calls += 1
        raise SlackApiError("rejected", _FakeResponse(self.code))


class _FakeResponse:
    """Minimal stand-in for slack_sdk's SlackResponse."""

    def __init__(self, code: str) -> None:
        """HTTP 200 plus an error code is Slack's definitive-rejection shape."""
        self.status_code, self._code = 200, code

    def get(self, key: str, default: object = None) -> object:
        """Return the error code the way SlackResponse's mapping access does."""
        return self._code if key == "error" else default


def test_systemic_slack_rejection_releases_the_lead(tmp_path: Path) -> None:
    """A wrong channel or revoked token is not this lead's fault. Treating Slack's
    definitive 'no' as ambiguous consumed a real lead per attempt — measured at 1-2 gold
    leads destroyed per weekday while nothing was posted. The lead must go back."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    client = _RejectingClient("channel_not_found")
    outcome = drip.run_drip(client, "C1", conn, force=True)
    assert outcome.startswith("blocked:") and "channel_not_found" in outcome
    # No LEAD-bearing row survives — the lead went back in the pool untouched.
    assert conn.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE lead_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert len(db.nugget_candidates(conn, "C1")) == 1, "a good lead was consumed"
    # ...and the channel is now blocked so later ticks stop instead of repeating.
    guard = db.channel_guard(conn, "C1")
    assert guard is not None and guard["state"] == "blocked"


def test_lead_specific_rejection_is_quarantined_not_released(tmp_path: Path) -> None:
    """A card Slack refuses on its own merits must not be retried forever either."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    outcome = drip.run_drip(_RejectingClient("msg_too_long"), "C1", conn, force=True)
    # "quarantined:", not "skip:" — a destroyed lead must not read as a routine tick,
    # and cli.FAILING_DRIP_OUTCOMES turns this into a non-zero exit.
    assert outcome.startswith("quarantined:") and "msg_too_long" in outcome
    assert conn.execute(
        "SELECT state FROM notification_outbox"
    ).fetchone()["state"] == "rejected"
    assert db.nugget_candidates(conn, "C1") == []
    assert len(db.blocked_notifications(conn)) == 1


def test_ambiguous_failure_still_burns_the_lead(tmp_path: Path) -> None:
    """The distinction is the invariant: ambiguous KEEPS the reservation (a duplicate is
    worse than a lost lead), definitive rejection releases it."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    assert drip.run_drip(_SlackClient(fail=True), "C1", conn, force=True).startswith(
        "unknown:"
    )
    assert conn.execute(
        "SELECT state FROM notification_outbox"
    ).fetchone()["state"] == "unknown"
    assert db.nugget_candidates(conn, "C1") == []


def test_unrenderable_lead_is_quarantined_and_the_next_one_posts(tmp_path: Path) -> None:
    """The renderers raise BEFORE any reservation exists, so nothing recorded the
    failure: the same top-ranked lead was re-picked every tick, the tick crashed with a
    traceback only cron.log saw, and the product went silent permanently."""
    conn = db.connect(tmp_path / "t.db")
    bad = _mk_lead(conn, iid="BAD", entity="Good District", amount=900_000.0,
                   start="2025-10-10", end="2028-09-30", backfill=True)
    _mk_lead(conn, iid="OK", entity="Next District", amount=400_000.0,
             start="2025-10-10", end="2028-09-30", backfill=True)
    # '***' sanitizes to an empty entity, so build_nugget raises.
    conn.execute("UPDATE leads SET entity_name='***' WHERE id=?", (bad,))
    conn.commit()
    client = _SlackClient()
    first = drip.run_drip(client, "C1", conn, force=True)
    assert first.startswith("quarantined:") and "cannot be rendered" in first
    assert client.calls == 0
    assert conn.execute(
        "SELECT state FROM notification_outbox"
    ).fetchone()["state"] == "unrenderable"
    second = drip.run_drip(client, "C1", conn, force=True)
    assert second.startswith("posted") and "Next District" in second


def test_a_playground_reservation_does_not_burn_a_production_lead(
    tmp_path: Path,
) -> None:
    """Both exclusions are audience-scoped, so testing in one channel cannot silently
    consume the other channel's inventory."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    assert drip.run_drip(_SlackClient(fail=True), "PLAYGROUND", conn,
                         force=True).startswith("unknown:")
    assert db.nugget_candidates(conn, "PLAYGROUND") == []
    assert len(db.nugget_candidates(conn, "PRODUCTION")) == 1


def test_allowlist_uses_exact_match_for_constant_state_sources() -> None:
    """Prefix-matching a constant-state source would trust a future 'webs-inferred'
    purely because of how it was named — the exact failure the allowlist prevents."""
    assert territory.state_is_verified("webs")
    assert not territory.state_is_verified("webs-inferred")
    assert not territory.state_is_verified("sam.gov-scraped")
    assert not territory.state_is_verified("ca-grants-portal-mirror")
    # Namespaced sources still match by prefix, because the suffix legitimately varies.
    assert territory.state_is_verified("usaspending:16.710")
    assert territory.state_is_verified("ca-grants-award:2023-2024")


# --------------------------------- channel guards, unknown codes, 429, dry-run honesty
class _RateLimitedClient:
    """Slack answering 429 with a Retry-After header."""

    def __init__(self, retry_after: str = "45") -> None:
        """Record the Retry-After value this client will answer with."""
        self.retry_after, self.calls = retry_after, 0

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:  # noqa: N802
        """Raise the way slack_sdk does when rate-limited."""
        self.calls += 1
        response = _FakeResponse("ratelimited")
        response.status_code = 429
        response.headers = {"Retry-After": self.retry_after}
        raise SlackApiError("ratelimited", response)


def test_systemic_failure_blocks_the_channel_for_later_ticks(tmp_path: Path) -> None:
    """A wrong channel or dead token must stop the drip, not fail identically every 30
    minutes. Each repeat previously consumed a lead; now the first one blocks."""
    conn = db.connect(tmp_path / "t.db")
    for index in range(3):
        _mk_lead(conn, iid=f"G{index}", entity=f"District {index}",
                 amount=500_000.0 - index, start="2025-10-10", end="2028-09-30",
                 backfill=True)
    client = _RejectingClient("invalid_auth")
    assert drip.run_drip(client, "C1", conn, force=True).startswith("blocked:")
    # Later ticks must not even attempt a post (force=False = a real cron tick).
    for _ in range(3):
        outcome = drip.run_drip(client, "C1", conn, force=False)
        assert outcome.startswith("blocked:"), outcome
    assert client.calls == 1, "blocked channel still called Slack"
    assert len(db.nugget_candidates(conn, "C1")) == 3, "leads were consumed while blocked"
    # The guard is time-BOUNDED, not permanent — a forever-block just trades one silent
    # wedge for another. An operator may still clear it early.
    guard = db.channel_guard(conn, "C1")
    assert guard is not None and guard["state"] == "blocked"
    assert str(guard["available_at"]) > str(guard["created_at"])
    assert db.clear_channel_guard(conn, "C1") is True
    assert db.channel_guard(conn, "C1") is None


def test_unknown_slack_error_code_releases_rather_than_quarantines(
    tmp_path: Path,
) -> None:
    """Not knowing what went wrong is NOT evidence the lead is unusable. Only
    explicitly allowlisted content errors may destroy inventory."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    outcome = drip.run_drip(_RejectingClient("some_new_slack_error"), "C1", conn,
                            force=True)
    assert outcome.startswith("error:") and "released, not quarantined" in outcome
    assert conn.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE lead_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert len(db.nugget_candidates(conn, "C1")) == 1
    assert db.channel_guard(conn, "C1") is None  # unknown != systemic


def test_rate_limit_backs_off_without_consuming_a_lead(tmp_path: Path) -> None:
    """429 is neither ambiguous nor this lead's fault. Respect Retry-After."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    client = _RateLimitedClient(retry_after="45")
    outcome = drip.run_drip(client, "C1", conn, force=True)
    assert outcome.startswith("backoff:") and "45s" in outcome
    assert len(db.nugget_candidates(conn, "C1")) == 1, "a lead was consumed by a 429"
    guard = db.channel_guard(conn, "C1")
    assert guard is not None and guard["state"] == "backoff"
    # A real tick holds rather than hammering the API.
    assert drip.run_drip(client, "C1", conn, force=False).startswith("backoff:")
    assert client.calls == 1


def test_lapsed_backoff_clears_itself(tmp_path: Path) -> None:
    """Unlike a block, a backoff is time-based and needs no operator."""
    conn = db.connect(tmp_path / "t.db")
    db.set_channel_guard(conn, "C1", "backoff", "ratelimited",
                         available_at="2000-01-01T00:00:00+00:00")
    assert db.channel_guard(conn, "C1") is None


def test_dry_run_does_not_claim_a_quarantine_happened(tmp_path: Path) -> None:
    """--dry-run writes NOTHING (CLAUDE.md), so it must not report that it did."""
    conn = db.connect(tmp_path / "t.db")
    bad = _mk_lead(conn, iid="BAD", start="2025-10-10", end="2028-09-30", backfill=True)
    conn.execute("UPDATE leads SET entity_name='***' WHERE id=?", (bad,))
    conn.commit()
    outcome = drip.run_drip(None, "C1", conn, force=True, dry_run=True)
    assert outcome.startswith("[dry-run]") and "WOULD quarantine" in outcome
    assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_assumed_provenance_sources_fail_closed() -> None:
    """`usaspending-subaward:` and `sam.gov` state semantics are ASSUMED, not evidenced,
    so they must post untagged until someone proves them."""
    assert not territory.state_is_verified("usaspending-subaward:97.008")
    assert not territory.state_is_verified("sam.gov")
    assert territory.mention_line("WA", "sam.gov") == ""
    # ca-grants-portal DOES reach production via bulletin_candidates and stays verified.
    assert territory.state_is_verified("ca-grants-portal")


def test_cmd_drip_exit_code_follows_the_real_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FUNCTIONAL replacement for a tautology. The old test asserted the constant
    against itself — it never called cmd_drip, never checked an exit code, and would
    have stayed green if a prefix were renamed while cron went green forever."""
    from grant_watch import cli

    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-used-dry-run")
    monkeypatch.setattr(db, "connect_readonly", lambda *a, **k: db.connect(tmp_path / "t.db"))

    def outcome_of(text: str) -> int:
        """Drive cmd_drip with a mocked run_drip and return its exit code."""
        monkeypatch.setattr(
            "grant_watch.slack.drip.run_drip", lambda *a, **k: text
        )
        return cli.cmd_drip(force=False, dry_run=True)

    assert outcome_of("blocked: channel is blocked (invalid_auth)") == 1
    assert outcome_of("unknown: Slack delivery could not be confirmed") == 1
    assert outcome_of("error: unrecognized code") == 1
    assert outcome_of("quarantined: lead #4 cannot be rendered") == 1
    assert outcome_of("posted nugget (award-brief) for lead #1: X") == 0
    assert outcome_of("skip: daily cap reached (1)") == 0


def test_expired_guard_is_ignored_without_being_deleted(tmp_path: Path) -> None:
    """C2 — `channel_guard` must be a PURE READ. It previously self-healed with a
    DELETE, which crashed `--dry-run` on the read-only connection cmd_drip opens, and
    on a writable connection silently wrote during a dry run."""
    conn = db.connect(tmp_path / "t.db")
    db.set_channel_guard(conn, "C1", "backoff", "ratelimited",
                         available_at="2000-01-01T00:00:00+00:00")
    assert db.channel_guard(conn, "C1") is None  # expired -> ignored by the query
    # ...but still present. Clearing belongs on an explicitly writable path.
    assert db.channel_guard_any(conn, "C1", "backoff") is not None


def test_dry_run_leaves_the_database_unchanged_with_an_expired_guard(
    tmp_path: Path,
) -> None:
    """C2 regression: a dry run must write NOTHING, even when a guard has lapsed."""
    path = tmp_path / "t.db"
    conn = db.connect(path)
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    db.set_channel_guard(conn, "C1", "backoff", "ratelimited",
                         available_at="2000-01-01T00:00:00+00:00")
    before = [tuple(r) for r in conn.execute("SELECT * FROM notification_outbox")]
    outcome = drip.run_drip(None, "C1", conn, force=True, dry_run=True)
    assert outcome.startswith("[dry-run]")
    after = [tuple(r) for r in conn.execute("SELECT * FROM notification_outbox")]
    assert after == before, "dry-run mutated notification_outbox"


def test_dry_run_survives_a_readonly_connection_with_an_expired_guard(
    tmp_path: Path,
) -> None:
    """The documented dry-run entrypoint opens `connect_readonly`. A lapsed guard used
    to raise `attempt to write a readonly database` there."""
    path = tmp_path / "t.db"
    writable = db.connect(path)
    _mk_lead(writable, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    db.set_channel_guard(writable, "C1", "backoff", "ratelimited",
                         available_at="2000-01-01T00:00:00+00:00")
    writable.close()
    readonly = db.connect_readonly(path)
    outcome = drip.run_drip(None, "C1", readonly, force=True, dry_run=True)
    assert outcome.startswith("[dry-run]"), outcome


def test_channel_guard_never_counts_as_a_delivery_or_consumes_the_cap(
    tmp_path: Path,
) -> None:
    """H2 — guard rows share `notification_outbox` with real reservations. Counting one
    produced `daily cap reached (1)` with ZERO posts and ZERO reservations, silently
    spending the day's only card."""
    conn = db.connect(tmp_path / "t.db")
    db.set_channel_guard(conn, "C1", "backoff", "ratelimited",
                         available_at="2099-01-01T00:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert db.delivery_attempts_today(conn, "C1") == []
    go, reason = drip.pacing_ok(conn, "C1", datetime(2026, 7, 22, 20, 0,
                                                     tzinfo=timezone.utc))
    assert go, f"a channel guard consumed the daily cap: {reason}"


def test_backoff_escalates_and_stays_capped() -> None:
    """Bounded AND escalating: a real outage must not be hammered every 30 minutes,
    and must not be blocked forever either."""
    assert drip._systemic_backoff_minutes(1) == 60
    assert drip._systemic_backoff_minutes(2) == 120
    assert drip._systemic_backoff_minutes(3) == 240
    assert drip._systemic_backoff_minutes(9) == drip._systemic_backoff_minutes(4) == 480


def test_a_successful_post_clears_the_guard(tmp_path: Path) -> None:
    """Recovery is automatic on the writable path: a confirmed delivery proves the
    channel works again."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    db.set_channel_guard(conn, "C1", "blocked", "invalid_auth",
                         available_at="2000-01-01T00:00:00+00:00")
    assert drip.run_drip(_SlackClient(), "C1", conn, force=True).startswith("posted")
    assert db.channel_guard_any(conn, "C1") is None


def test_drip_blocked_renders_guards_and_leads_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C-1 REGRESSION. `cli drip-blocked` crashed with IndexError the moment a guard
    existed — `available_at` was missing from the projection while the renderer printed
    it. The operator's only window into an outage failed during the outage, and the
    crash also aborted before the quarantined-lead section, hiding those too.
    Untested code: `cmd_drip_blocked` previously had zero coverage."""
    from grant_watch import cli

    path = tmp_path / "t.db"
    conn = db.connect(path)
    lead_id = _mk_lead(conn, iid="Q1", entity="Quarantined District",
                       start="2025-10-10", end="2028-09-30", backfill=True)
    db.quarantine_lead(conn, lead_id, None, "C1", "nugget", "unrenderable: no entity")
    db.set_channel_guard(conn, "C1", "blocked", "invalid_auth",
                         available_at="2099-01-01T00:00:00+00:00")
    conn.close()
    original = db.connect_readonly
    db.connect_readonly = lambda *a, **k: db.connect(path)  # type: ignore[assignment]
    try:
        assert cli.cmd_drip_blocked() == 0
    finally:
        db.connect_readonly = original  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert "CHANNEL GUARD" in out and "invalid_auth" in out
    assert "holds_until=2099-01-01T00:00:00+00:00" in out
    assert "drip-unblock" in out  # the remedy is named
    assert "Quarantined District" in out, "a guard hid the quarantined leads"


def test_a_new_incident_escalates_from_the_beginning(tmp_path: Path) -> None:
    """H-1 REGRESSION, proven against the DB rather than the pure function.

    `_incident_lapsed` computed a fresh attempts=1 in Python, and `set_channel_guard`'s
    upsert then did `attempts+1` onto the stale row and never reset `created_at`. Both
    symptoms returned on the SECOND tick: escalation jumped to the 8-hour cap, and
    `first_failure` reported an outage starting months before the outage."""
    conn = db.connect(tmp_path / "t.db")
    # A long-finished incident: 4 periods deep, expired months ago.
    db.set_channel_guard(conn, "C1", "blocked", "invalid_auth",
                         available_at="2026-04-23T00:00:00+00:00")
    for _ in range(3):
        db.set_channel_guard(conn, "C1", "blocked", "invalid_auth",
                             available_at="2026-04-23T00:00:00+00:00")
    stale = db.channel_guard_any(conn, "C1", "blocked")
    assert stale is not None and int(stale["attempts"]) >= 4

    for _ in range(2):  # two ticks of a brand-new incident
        _mk_lead(conn, iid=f"N{_}", entity=f"District {_}", amount=500_000.0 - _,
                 start="2025-10-10", end="2028-09-30", backfill=True)
        drip.run_drip(_RejectingClient("invalid_auth"), "C1", conn, force=True)
    fresh = db.channel_guard_any(conn, "C1", "blocked")
    assert fresh is not None
    # Second tick of a NEW incident is the 2nd period, not the 5th/6th.
    assert int(fresh["attempts"]) == 2, "the upsert overwrote the incident reset"
    assert not str(fresh["created_at"]).startswith("2026-04"), (
        "first_failure inherited a months-old date"
    )


def test_a_backoff_never_masks_an_active_block(tmp_path: Path) -> None:
    """A block needs a human and exits non-zero; a backoff is routine and exits 0.
    Ordering purely by expiry let a 429 hide a live credential outage behind a benign
    line for up to an hour — and `drip --force` is exactly how an operator trips it."""
    conn = db.connect(tmp_path / "t.db")
    db.set_channel_guard(conn, "C1", "blocked", "invalid_auth",
                         available_at="2099-01-01T00:00:00+00:00")
    db.set_channel_guard(conn, "C1", "backoff", "ratelimited",
                         available_at="2099-06-01T00:00:00+00:00")  # expires LATER
    guard = db.channel_guard(conn, "C1")
    assert guard is not None and guard["state"] == "blocked"
    outcome = drip.run_drip(_SlackClient(), "C1", conn, force=False)
    assert outcome.startswith("blocked:"), outcome


def test_a_short_backoff_cannot_shorten_or_inflate_a_long_block(
    tmp_path: Path,
) -> None:
    """Separate rows per failure class: two unrelated conditions must not corrupt each
    other's escalation."""
    conn = db.connect(tmp_path / "t.db")
    db.set_channel_guard(conn, "C1", "blocked", "invalid_auth",
                         available_at="2099-01-01T00:00:00+00:00")
    before = db.channel_guard_any(conn, "C1", "blocked")
    assert before is not None
    db.set_channel_guard(conn, "C1", "backoff", "ratelimited",
                         available_at="2026-07-22T00:00:30+00:00")
    after = db.channel_guard_any(conn, "C1", "blocked")
    assert after is not None
    assert after["attempts"] == before["attempts"]
    assert after["available_at"] == before["available_at"]


def test_drip_unblock_does_not_discard_an_active_rate_limit(tmp_path: Path) -> None:
    """`drip-unblock` means "the channel/token is fixed" — it says nothing about
    Slack's rate limiter, and clearing it would send the next tick into the 429."""
    conn = db.connect(tmp_path / "t.db")
    db.set_channel_guard(conn, "C1", "blocked", "invalid_auth",
                         available_at="2099-01-01T00:00:00+00:00")
    db.set_channel_guard(conn, "C1", "backoff", "ratelimited",
                         available_at="2099-01-01T00:00:00+00:00")
    assert db.clear_channel_guard(conn, "C1") is True  # default: blocked only
    assert db.channel_guard_any(conn, "C1", "blocked") is None
    assert db.channel_guard_any(conn, "C1", "backoff") is not None
