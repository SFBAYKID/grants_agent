"""Drip failure modes that silence or misdirect the product.

Split from test_drip.py, which is near the 1000-line cap (Constitution rule 4).
test_drip.py owns WHAT Grant says; this file owns the ways it can stop saying anything,
or say it to the wrong person. Every test here corresponds to a defect that was proven
against a real database before it was fixed — none is speculative.
"""

from __future__ import annotations

from datetime import date, time
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
    assert outcome.startswith("halt:") and "channel_not_found" in outcome
    assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    assert len(db.nugget_candidates(conn, "C1")) == 1, "a good lead was consumed"


def test_lead_specific_rejection_is_quarantined_not_released(tmp_path: Path) -> None:
    """A card Slack refuses on its own merits must not be retried forever either."""
    conn = db.connect(tmp_path / "t.db")
    _mk_lead(conn, iid="G1", start="2025-10-10", end="2028-09-30", backfill=True)
    outcome = drip.run_drip(_RejectingClient("msg_too_long"), "C1", conn, force=True)
    assert outcome.startswith("skip:") and "msg_too_long" in outcome
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
    assert first.startswith("skip:") and "cannot be rendered" in first
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
