"""Snapshot-bound action authorization, idempotency, freshness, and wire tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grant_watch import persequor_client
from grant_watch.campaign import actions, delivery
from grant_watch.slack import grant
from tests.test_rich_delivery import FakeSlack, READY
from tests.test_rich_preparation import _eligible_conn


@pytest.fixture(autouse=True)
def _workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the exact workspace identity required by every action."""
    monkeypatch.setenv("SLACK_WORKSPACE_ID", "TWORKSPACE")


def _posted(path: Path) -> tuple[sqlite3.Connection, sqlite3.Row]:
    """Create one locally delivered snapshot/post using fake Slack."""
    conn = _eligible_conn(path)
    outcome = delivery.run(
        FakeSlack(conn),
        "CGRANTS",
        conn,
        channel_members=frozenset({"U01DFJWQQJ3"}),
        force=True,
        now=READY,
    )
    assert outcome.startswith("posted rich_award")
    post = conn.execute("SELECT * FROM posts").fetchone()
    return conn, post


def _kwargs(post: sqlite3.Row) -> dict[str, object]:
    """Return one valid action context for the fixture post."""
    return {
        "workspace": "TWORKSPACE",
        "channel": "CGRANTS",
        "thread_ts": str(post["ts"]),
        "requester": "U01DFJWQQJ3",
        "requester_is_member": True,
        "nonce": "action-event-1",
    }


def test_draft_persists_before_one_submit_and_uses_exact_wire_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The external call sees durable requested state and unchanged v1 key set."""
    conn, post = _posted(tmp_path / "draft.db")
    monkeypatch.delenv("OUTREACH_TEST_EMAIL", raising=False)
    captured: list[persequor_client.OutreachBrief] = []

    def submit(
        conn_: sqlite3.Connection,
        lead_id: int,
        brief: persequor_client.OutreachBrief,
    ) -> tuple[str, str]:
        """Assert durable reservation and capture the frozen wire payload."""
        assert conn_ is conn and lead_id == 1
        assert (
            conn.execute("SELECT state FROM rich_card_actions").fetchone()[0]
            == "requested"
        )
        captured.append(brief)
        return "submitted", "Persequor accepted the draft request."

    result = actions.request_draft(
        conn, str(post["snapshot_id"]), submitter=submit, **_kwargs(post)
    )
    assert result.state == "accepted"
    assert len(captured) == 1
    brief = captured[0]
    assert set(brief) == set(persequor_client.OutreachBrief.__required_keys__)
    assert brief["entity_type"] == "school_district"
    assert brief["amount_usd"] == 500_000
    assert brief["contact_email"] == "jon@montebello.k12.ca.us"
    assert brief["schema"] == "outreach-request.v1"


def test_double_click_and_slack_replay_submit_once(tmp_path: Path) -> None:
    """Different action nonces still collapse to one draft per immutable snapshot."""
    conn, post = _posted(tmp_path / "double.db")
    calls = 0

    def submit(*_args: object) -> tuple[str, str]:
        """Count fake external submissions."""
        nonlocal calls
        calls += 1
        return "submitted", "accepted"

    first = actions.request_draft(
        conn, str(post["snapshot_id"]), submitter=submit, **_kwargs(post)
    )
    replay = {**_kwargs(post), "nonce": "action-event-2"}
    second = actions.request_draft(
        conn, str(post["snapshot_id"]), submitter=submit, **replay
    )
    assert first.state == "accepted" and second.state == "accepted"
    assert calls == 1
    assert conn.execute("SELECT COUNT(*) FROM rich_card_actions").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({"workspace": ""}, "workspace"),
        ({"channel": "COTHER"}, "channel/thread"),
        ({"thread_ts": "wrong"}, "channel/thread"),
        ({"requester": "UUNKNOWN"}, "requester"),
        ({"requester_is_member": False}, "requester"),
    ],
)
def test_wrong_context_or_user_is_rejected_before_state(
    tmp_path: Path, changed: dict[str, object], message: str
) -> None:
    """Workspace/channel/thread/roster/membership mismatches create no action."""
    conn, post = _posted(tmp_path / f"wrong-{message.replace('/', '-')}.db")
    kwargs = {**_kwargs(post), **changed}
    with pytest.raises(PermissionError, match=message):
        actions.request_draft(
            conn,
            str(post["snapshot_id"]),
            submitter=lambda *_args: ("submitted", "bad"),
            **kwargs,
        )
    assert conn.execute("SELECT COUNT(*) FROM rich_card_actions").fetchone()[0] == 0


def test_expired_or_removed_contact_blocks_without_submit(tmp_path: Path) -> None:
    """Click-time freshness veto requests reverification instead of using stale PII."""
    conn, post = _posted(tmp_path / "expired.db")
    conn.execute("UPDATE contact_evidence SET status='removed'")
    conn.commit()
    called = False

    def submit(*_args: object) -> tuple[str, str]:
        """Fail the test if stale contact evidence reaches submission."""
        nonlocal called
        called = True
        return "submitted", "bad"

    result = actions.request_draft(
        conn,
        str(post["snapshot_id"]),
        submitter=submit,
        now=datetime(2026, 7, 23, tzinfo=timezone.utc),
        **_kwargs(post),
    )
    assert result.state == "blocked_expired"
    assert called is False
    assert conn.execute("SELECT COUNT(*) FROM rich_card_actions").fetchone()[0] == 0


def test_requested_action_resumes_idempotent_submit_after_crash(tmp_path: Path) -> None:
    """A crash gap before outbox creation remains safely resumable on Slack retry."""
    conn, post = _posted(tmp_path / "resume.db")
    now = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO rich_card_actions
             (id,snapshot_id,action,nonce,requester_slack,state,created_at,updated_at)
           VALUES ('reserved',?,'draft','old-event','U01DFJWQQJ3','requested',?,?)""",
        (post["snapshot_id"], now, now),
    )
    conn.commit()
    calls = 0

    def submit(*_args: object) -> tuple[str, str]:
        """Resume the stable idempotent submission once."""
        nonlocal calls
        calls += 1
        return "submitted", "accepted"

    result = actions.request_draft(
        conn,
        str(post["snapshot_id"]),
        submitter=submit,
        **{**_kwargs(post), "nonce": "retry-event"},
    )
    assert result.state == "accepted" and calls == 1
    row = conn.execute("SELECT id,state FROM rich_card_actions").fetchone()
    assert tuple(row) == ("reserved", "accepted")


def test_not_relevant_is_deduplicated_and_legacy_visible(tmp_path: Path) -> None:
    """Feedback updates the legacy lead status once so rollback cannot repost it."""
    conn, post = _posted(tmp_path / "irrelevant.db")
    first = actions.mark_not_relevant(conn, str(post["snapshot_id"]), **_kwargs(post))
    second = actions.mark_not_relevant(
        conn,
        str(post["snapshot_id"]),
        **{**_kwargs(post), "nonce": "action-event-2"},
    )
    assert first.state == second.state == "accepted"
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "not_relevant"
    assert conn.execute("SELECT COUNT(*) FROM rich_card_actions").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM outcome_events WHERE kind='not_relevant'"
        ).fetchone()[0]
        == 1
    )


def test_rich_thread_question_uses_frozen_snapshot_not_mutable_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary thread handler receives frozen event/contact/CRM context after drift."""
    from grant_watch.slack import conversation

    conn, post = _posted(tmp_path / "thread.db")
    conn.execute(
        "UPDATE leads SET entity_name='Mutated Entity',amount=1,current_event_id=999"
    )
    conn.commit()
    seen: dict[str, object] = {}

    def respond(
        _text: str, row: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        """Capture the immutable context passed to the offline responder."""
        seen.update(row)
        return {"intent": "question", "reply": "Frozen answer", "files": []}

    class Status:
        """Avoid real spinner calls while preserving handler flow."""

        def __init__(self, *_args: object) -> None:
            """Initialize the no-op status helper."""
            pass

        def start(self) -> None:
            """Start no external status activity."""
            pass

        def update(self, _text: str) -> None:
            """Ignore deterministic status text."""
            pass

        def finalize(self, _text: str, _blocks: object = None) -> bool:
            """Report that the local response finalized."""
            return True

    class Client:
        """Return one offline thread history page."""

        def conversations_replies(self, **_kwargs: object) -> dict[str, object]:
            """Return an empty offline Slack thread."""
            return {"messages": []}

    monkeypatch.setattr(conversation, "respond", respond)
    monkeypatch.setattr(grant, "_Status", Status)
    event = {
        "user": "U01DFJWQQJ3",
        "text": "What is this?",
        "channel": "CGRANTS",
        "ts": "172.001",
    }
    assert grant._handle_drip_thread(
        conn, post, event, lambda **_kwargs: None, Client(), workspace="TWORKSPACE"
    )
    assert seen["entity_name"] == "Montebello USD"
    assert seen["amount"] == 500_000
    assert seen["current_event_id"] == 2
