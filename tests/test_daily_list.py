"""The daily list: freshest first, and nothing ever repeats.

Chase's rule was "we are always checking for fresh data, and if we already posted it
we slowly go back". Walking backwards is not implemented anywhere — it falls out of
ordering by award date descending and skipping what a channel has already seen. These
tests pin that, because the day it stops being true the same 25 cards post forever.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from grant_watch import db, db_engagement
from grant_watch.campaign import preparation
from grant_watch.slack import daily_list

from drip_support import mk_lead

CHANNEL = "C0B02721MNK"
DAY_ONE = datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)


class FakeSlack:
    """Records the one call the list makes, and can be told to fail."""

    def __init__(self, error: str = "") -> None:
        """Set up a client that succeeds, or fails with one Slack error code."""
        self.error = error
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        """Mimic Slack: return a ts, or raise the configured error."""
        self.calls += 1
        self.kwargs = kwargs
        if self.error:
            raise SlackApiError("nope", {"error": self.error})
        return {"ts": "1788300000.000100"}


@pytest.fixture()
def pool(tmp_path: Path) -> tuple[sqlite3.Connection, list[int]]:
    """Five awards, each a day fresher than the last, newest = highest id."""
    conn = db.connect(tmp_path / "list.db")
    ids = [
        mk_lead(conn, iid=f"A{n}", entity=f"District {n}", start=f"2026-08-0{n}")
        for n in range(1, 6)
    ]
    return conn, ids


def _names(rows: list[sqlite3.Row]) -> list[str]:
    """Entity names, for readable assertions."""
    return [str(row["entity_name"]) for row in rows]


def test_the_list_walks_backwards_without_any_backfill_logic(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """Three days running, no repeats, oldest reached last.

    THE WHOLE FEATURE IN ONE TEST. If this fails, the same cards post every morning.
    """
    conn, _ids = pool
    client = FakeSlack()
    seen: list[str] = []
    for offset in range(3):
        daily_list.run(
            client, CHANNEL, conn, limit=2, now=DAY_ONE + timedelta(days=offset)
        )
        listed = conn.execute(
            "SELECT l.entity_name FROM daily_list_items i JOIN leads l ON l.id=i.lead_id"
            " ORDER BY i.id"
        ).fetchall()
        seen = [str(r[0]) for r in listed]
    assert len(seen) == len(set(seen)), "a lead must never be listed twice"
    assert seen[:2] == ["District 5", "District 4"], "freshest first"
    assert seen[-1] == "District 1", "and it reaches the oldest last"


def test_one_list_a_day_per_channel(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """A cron that ticks twice, or a retry, must not post a second list."""
    conn, _ids = pool
    client = FakeSlack()
    assert daily_list.run(client, CHANNEL, conn, limit=2, now=DAY_ONE).startswith(
        "posted"
    )
    assert "already had its list today" in daily_list.run(
        client, CHANNEL, conn, limit=2, now=DAY_ONE
    )
    assert client.calls == 1


def test_a_listed_lead_never_comes_back_as_a_card(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """BOTH card paths, because they are different queries in different files.

    The list has its own ledger rather than `posts`, so the existing "already posted"
    exclusions cannot see it. Without an explicit predicate every listed lead returns
    as a card the following day.
    """
    conn, _ids = pool
    daily_list.run(FakeSlack(), CHANNEL, conn, limit=4, now=DAY_ONE)
    listed = {int(r[0]) for r in conn.execute("SELECT lead_id FROM daily_list_items")}
    assert len(listed) == 4
    nuggets = {int(r["id"]) for r in db_engagement.nugget_candidates(conn, CHANNEL)}
    rich = {int(r["id"]) for r in preparation._rows(conn, CHANNEL, 50)}
    assert not (nuggets & listed), "the legacy card is offering a listed lead"
    assert not (rich & listed), "the rich card is offering a listed lead"
    assert nuggets and rich, "the control: the unlisted lead is still offered"


def test_another_channel_has_its_own_memory(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """The ledger is per channel, so a test channel cannot silently consume the pool."""
    conn, _ids = pool
    daily_list.run(FakeSlack(), CHANNEL, conn, limit=5, now=DAY_ONE)
    assert daily_list.candidates(conn, CHANNEL, 5) == []
    assert len(daily_list.candidates(conn, "C0BSDPM2KPB", 5)) == 5


def test_a_refused_list_releases_its_leads(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """Slack saying no must not spend the leads on a message nobody received."""
    conn, _ids = pool
    outcome = daily_list.run(
        FakeSlack("channel_not_found"), CHANNEL, conn, limit=3, now=DAY_ONE
    )
    assert "released" in outcome
    assert conn.execute("SELECT COUNT(*) FROM daily_list_items").fetchone()[0] == 0
    assert len(daily_list.candidates(conn, CHANNEL, 5)) == 5


def test_an_ambiguous_send_keeps_the_leads_and_never_retries(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """A 5xx may in fact have been delivered.

    Releasing would post a second copy of somebody's daily list, which is worse than
    posting none — the never-blind-retry rule the outbox already enforces.
    """
    conn, _ids = pool
    outcome = daily_list.run(
        FakeSlack("service_unavailable"), CHANNEL, conn, limit=3, now=DAY_ONE
    )
    assert outcome.startswith("unknown")
    held = {
        int(r[0])
        for r in conn.execute(
            "SELECT lead_id FROM daily_list_items WHERE state='unknown'"
        )
    }
    assert len(held) == 3
    # The three stay spoken for; the two the list never reached are still available.
    # Asserting an EMPTY candidate set here would pass for the wrong reason on a
    # five-lead pool and hide a released reservation.
    remaining = {int(r["id"]) for r in daily_list.candidates(conn, CHANNEL, 5)}
    assert not (remaining & held), "an ambiguous send must never be re-listed"
    assert len(remaining) == 2


def test_an_undated_award_is_never_listed(tmp_path: Path) -> None:
    """A list headed "newest awards" cannot contain a lead with no date."""
    conn = db.connect(tmp_path / "undated.db")
    lead_id = mk_lead(conn)
    conn.execute(
        "UPDATE funding_events SET occurred_on=NULL WHERE id="
        "(SELECT current_event_id FROM leads WHERE id=?)",
        (lead_id,),
    )
    conn.commit()
    assert daily_list.candidates(conn, CHANNEL, 5) == []


def test_an_amountless_award_is_never_listed(tmp_path: Path) -> None:
    """The renderer's precondition, carried in the query that feeds it."""
    conn = db.connect(tmp_path / "free.db")
    lead_id = mk_lead(conn)
    conn.execute("UPDATE leads SET amount=0 WHERE id=?", (lead_id,))
    conn.commit()
    assert daily_list.candidates(conn, CHANNEL, 5) == []


def test_a_claimed_lead_is_never_listed(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """A rep who took a lead is not shown it again the next morning."""
    from grant_watch import lead_claims

    conn, _ids = pool
    org = next(o for o in lead_claims.resolve(conn, "District 5"))
    lead_claims.claim(
        conn,
        org,
        slack_user="U01E908206M",
        audience=CHANNEL,
        thread_ts="1.0",
        message_ts="1.0",
        claim_text="I'm taking District 5",
        now=DAY_ONE,
    )
    assert "District 5" not in _names(daily_list.candidates(conn, CHANNEL, 5))
    assert "District 4" in _names(daily_list.candidates(conn, CHANNEL, 5))


def test_a_dry_run_reserves_nothing(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """Observing the list must not consume it."""
    conn, _ids = pool
    client = FakeSlack()
    outcome = daily_list.run(client, CHANNEL, conn, limit=3, dry_run=True, now=DAY_ONE)
    assert outcome.startswith("[dry-run]")
    assert client.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM daily_list_items").fetchone()[0] == 0


def test_an_empty_pool_says_so_rather_than_posting_nothing(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """Draining the pool is a reportable state, not silence."""
    conn, _ids = pool
    daily_list.run(FakeSlack(), CHANNEL, conn, limit=5, now=DAY_ONE)
    assert (
        daily_list.run(
            FakeSlack(), CHANNEL, conn, limit=5, now=DAY_ONE + timedelta(days=1)
        )
        == "skip: nothing unseen to list"
    )


def test_every_card_states_the_award_age(
    pool: tuple[sqlite3.Connection, list[int]],
) -> None:
    """The reason this feature exists, asserted on the rendered blocks."""
    conn, _ids = pool
    client = FakeSlack()
    daily_list.run(client, CHANNEL, conn, limit=3, now=DAY_ONE)
    rendered = str(client.kwargs["blocks"])
    assert rendered.count("ago") >= 3
    assert "obligated" in rendered


def test_the_blocks_stay_inside_slack_s_ceiling(tmp_path: Path) -> None:
    """`invalid_blocks` RELEASES the whole list, so 30 cards must not post nothing."""
    conn = db.connect(tmp_path / "big.db")
    for n in range(30):
        mk_lead(conn, iid=f"B{n}", entity=f"District {n}")
    rows = daily_list.candidates(conn, CHANNEL, 30)
    assert len(rows) == 30
    blocks = daily_list.build_blocks(rows, date(2026, 9, 2))
    assert len(blocks) <= 50
