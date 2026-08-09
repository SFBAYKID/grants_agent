"""Proactive follow-ups: what Grant may claim, and how often it may say it.

Two failure modes matter more than anything else here. The first is a false claim in
a team channel — silence in Slack is not evidence a rep did nothing, so a nudge may
only report what Grant observed in its own records. The second is nagging: a rep who
parked something deliberately must not be asked about it every morning.

Both are enforced structurally rather than by wording discipline. One nudge per
subject is a UNIQUE constraint, and every claim is re-verified inside the same call
that reserves the send, so anything resolved while queued produces silence.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


from grant_watch import db
from grant_watch.slack import nudges

CHANNEL = "C0TEST"
REP = "U0REP"
# A Wednesday inside the business window (11:00 Pacific).
NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


class _Client:
    """Slack stand-in recording posts, or failing the way Slack does."""

    def __init__(self, error: str = "") -> None:
        """Record posts, or raise the named Slack error on every send."""
        self.posts: list[dict[str, Any]] = []
        self.error = error

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        """Record one post or raise a SlackApiError with the configured code."""
        if self.error:
            from slack_sdk.errors import SlackApiError

            raise SlackApiError(self.error, {"error": self.error})
        self.posts.append(kwargs)
        return {"ok": True, "ts": "999.1"}


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database."""
    return db.connect(tmp_path / "n.db")


def _expired_preview(conn: sqlite3.Connection, when: datetime) -> None:
    """Insert a ready preview whose approval window has already lapsed."""
    conn.execute(
        """INSERT INTO crm_actions
             (id,action_type,workspace,channel,thread_ts,requested_by,state,
              payload_json,payload_hash,nonce_hash,expires_at,created_at,updated_at)
           VALUES ('act-1','add_campaign_members','T1',?,'100.1',?,'ready',
                   '{}','h','n',?,?,?)""",
        (CHANNEL, REP, when.isoformat(), when.isoformat(), when.isoformat()),
    )
    conn.commit()


def test_an_abandoned_preview_is_followed_up_once_and_only_once(
    tmp_path: Path,
) -> None:
    """The one-shot rule: a second tick must not produce a second nudge."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    client = _Client()

    first = nudges.run(client, conn, now=NOW)
    assert "nudged crm_preview_expired" in first
    assert len(client.posts) == 1
    assert client.posts[0]["thread_ts"] == "100.1"
    assert client.posts[0]["channel"] == CHANNEL

    second = nudges.run(client, conn, now=NOW + timedelta(days=1))
    assert "nothing to follow up" in second
    assert len(client.posts) == 1
    conn.close()


def test_the_message_reports_what_grant_saw_and_never_asserts_inaction(
    tmp_path: Path,
) -> None:
    """Grant cannot see a phone call, so it must not imply none happened."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    client = _Client()
    nudges.run(client, conn, now=NOW)
    text = str(client.posts[0]["text"]).lower()
    assert "timed out before anyone hit the button" in text
    assert "nothing got written" in text
    for forbidden in ("you didn't", "you did not", "you never", "you forgot"):
        assert forbidden not in text
    conn.close()


def test_a_subject_that_resolved_while_queued_produces_silence(
    tmp_path: Path,
) -> None:
    """Re-verification happens at send time, not when the candidate was found."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    conn.execute("UPDATE crm_actions SET state='complete' WHERE id='act-1'")
    conn.commit()
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, now=NOW)
    assert client.posts == []
    conn.close()


def test_something_too_old_is_dropped_rather_than_mentioned_late(
    tmp_path: Path,
) -> None:
    """A nudge about three-week-old work is noise; it is recorded, not posted."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=20))
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, now=NOW)
    assert client.posts == []
    row = conn.execute("SELECT state,suppress_reason FROM followup_nudges").fetchone()
    assert row["state"] == "suppressed"
    assert row["suppress_reason"] == "stale"
    conn.close()


def test_a_dry_run_writes_absolutely_nothing(tmp_path: Path) -> None:
    """A command that posts to a team channel must be inert without --execute."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    client = _Client()
    outcome = nudges.run(client, conn, dry_run=True, now=NOW)
    assert outcome.startswith("[dry-run] would nudge")
    assert client.posts == []
    assert conn.execute("SELECT COUNT(*) FROM followup_nudges").fetchone()[0] == 0
    conn.close()


def test_the_reservation_is_committed_before_slack_is_called(
    tmp_path: Path,
) -> None:
    """A crash mid-send must not let the next tick post a duplicate."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))

    class _Exploding:
        """Fails after the reservation, the way a network drop would."""

        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            """Confirm a row already exists, then fail."""
            count = conn.execute(
                "SELECT COUNT(*) FROM followup_nudges WHERE state='reserved'"
            ).fetchone()[0]
            assert count == 1, "the send must not precede its reservation"
            raise RuntimeError("connection reset")

    outcome = nudges.run(_Exploding(), conn, now=NOW)
    assert "ambiguous" in outcome
    row = conn.execute("SELECT state FROM followup_nudges").fetchone()
    # Ambiguity is preserved, never retried: a duplicate nudge is worse than none.
    assert row["state"] == "unknown"
    assert "nothing to follow up" in nudges.run(_Client(), conn, now=NOW)
    conn.close()


def test_a_deleted_thread_is_suppressed_and_never_retried(tmp_path: Path) -> None:
    """thread_not_found is permanent; retrying it forever helps nobody."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    outcome = nudges.run(_Client(error="thread_not_found"), conn, now=NOW)
    assert "thread_not_found" in outcome
    assert conn.execute("SELECT state FROM followup_nudges").fetchone()[0] == (
        "suppressed"
    )
    conn.close()


def test_only_one_person_is_nudged_per_day(tmp_path: Path) -> None:
    """A nudge is a phone notification; two in a day for one rep is pestering."""
    conn = _conn(tmp_path)
    for index in (1, 2):
        conn.execute(
            """INSERT INTO crm_actions
                 (id,action_type,workspace,channel,thread_ts,requested_by,state,
                  payload_json,payload_hash,nonce_hash,expires_at,created_at,updated_at)
               VALUES (?,'add_campaign_members','T1',?,?,?,'ready','{}','h','n',?,?,?)""",
            (
                f"act-{index}",
                CHANNEL,
                f"10{index}.1",
                REP,
                (NOW - timedelta(hours=6)).isoformat(),
                (NOW - timedelta(hours=6)).isoformat(),
                (NOW - timedelta(hours=6)).isoformat(),
            ),
        )
    conn.commit()
    client = _Client()
    assert "nudged" in nudges.run(client, conn, now=NOW)
    second = nudges.run(client, conn, now=NOW + timedelta(minutes=5))
    assert "skip:" in second
    assert len(client.posts) == 1
    conn.close()


def test_nothing_is_sent_outside_business_hours(tmp_path: Path) -> None:
    """03:00 Pacific is not when a teammate should be pinged about a campaign."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=1))
    client = _Client()
    night = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)  # 03:00 PT
    assert "skip:" in nudges.run(client, conn, now=night)
    assert client.posts == []
    conn.close()


def test_a_parked_lead_is_not_chased(tmp_path: Path) -> None:
    """A rep who marked a lead dead has answered the question already."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,detail_url,status) "
        "VALUES (1,'s','1','Hoxie School District','u','dead')"
    )
    conn.execute(
        "INSERT INTO posts (id,kind,lead_id,channel,ts,posted_at) "
        "VALUES (1,'nugget',1,?,'50.1',?)",
        (CHANNEL, (NOW - timedelta(days=5)).isoformat()),
    )
    conn.commit()
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, now=NOW)
    assert client.posts == []
    assert (
        conn.execute("SELECT suppress_reason FROM followup_nudges").fetchone()[0]
        == "lead_parked"
    )
    conn.close()


def test_an_engaged_card_is_not_chased(tmp_path: Path) -> None:
    """Someone already replied; there is nothing to follow up on."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,detail_url,status) "
        "VALUES (1,'s','1','Hoxie School District','u','new')"
    )
    conn.execute(
        "INSERT INTO posts (id,kind,lead_id,channel,ts,posted_at) "
        "VALUES (1,'nugget',1,?,'50.1',?)",
        (CHANNEL, (NOW - timedelta(days=5)).isoformat()),
    )
    conn.execute(
        "INSERT INTO engagement (post_id,slack_user,kind,at) VALUES (1,?,'reply',?)",
        (REP, NOW.isoformat()),
    )
    conn.commit()
    assert "nothing to follow up" in nudges.run(_Client(), conn, now=NOW)
    conn.close()


def test_a_card_nudge_names_no_one(tmp_path: Path) -> None:
    """A card belongs to the channel, so nobody is singled out in public."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,detail_url,status) "
        "VALUES (1,'s','1','HOXIE SCHOOL DISTRICT NO 46','u','new')"
    )
    conn.execute(
        "INSERT INTO posts (id,kind,lead_id,channel,ts,posted_at) "
        "VALUES (1,'nugget',1,?,'50.1',?)",
        (CHANNEL, (NOW - timedelta(days=5)).isoformat()),
    )
    conn.commit()
    client = _Client()
    assert "nudged card_unengaged" in nudges.run(client, conn, now=NOW)
    text = str(client.posts[0]["text"])
    assert "<@" not in text
    assert "only what I can see" in text
    conn.close()


def test_every_declared_subject_kind_has_a_message(tmp_path: Path) -> None:
    """A subject with no wording would post an empty or generic nudge."""
    for kind in nudges.NUDGE_SUBJECT_KINDS:
        candidate = nudges.NudgeCandidate(
            subject_kind=kind,
            subject_id="x",
            audience=CHANNEL,
            target_slack=REP,
            anchor_ts="1.1",
            stalled_at=NOW,
            observed={"entity_name": "Test District", "organizations": 3},
        )
        message = nudges.build_message(candidate)
        assert len(message) > 40, kind
        assert "?" in message, kind
