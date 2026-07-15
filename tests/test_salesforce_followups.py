"""Safety tests for the read-only Salesforce follow-up Slack monitor."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grant_watch import db
from grant_watch.slack import salesforce_followups as followups


class FakeSlack:
    """Capture Slack calls without external delivery."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:
        """Record one post or simulate an ambiguous transport failure."""
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError("ambiguous")
        return {"ts": "123.456"}


def _eligible(tmp_path: Path) -> tuple[Any, followups.FollowupCandidate]:
    """Create one locally audited Grant Campaign/member provenance chain."""
    conn = db.connect(tmp_path / "test.db")
    joined = "2026-07-10T17:00:00+00:00"
    conn.execute(
        """INSERT INTO leads(id,source,source_item_id,entity_name,first_seen,last_seen)
           VALUES (1,'test','1','MT ADAMS SCHOOL DISTRICT',?,?)""", (joined, joined))
    conn.execute(
        """INSERT INTO crm_actions
           (id,action_type,workspace,channel,thread_ts,requested_by,state,payload_json,
            payload_hash,nonce_hash,expires_at,committed_at,campaign_id,created_at,updated_at)
           VALUES ('create','create_campaign','T','C','1.1','U','complete','{}',
                   'h','n','2026-08-01T00:00:00+00:00',?,'701TEST',?,?)""",
        (joined, joined, joined))
    payload = json.dumps({"campaign": {"name": "Just Testing"}})
    conn.execute(
        """INSERT INTO crm_actions
           (id,action_type,workspace,channel,thread_ts,requested_by,state,payload_json,
            payload_hash,nonce_hash,expires_at,committed_at,campaign_id,created_at,updated_at)
           VALUES ('members','add_campaign_members','T','C','1.1','U','complete',?,
                   'h2','n2','2026-08-01T00:00:00+00:00',?,'701TEST',?,?)""",
        (payload, joined, joined, joined))
    proposed = json.dumps({"salesforce_ref": None, "proposed_lead": {"Company": "Mt Adams"}})
    conn.execute(
        """INSERT INTO crm_action_items
           (action_id,lead_id,canonical_entity_key,operation,proposed_json,state,
            salesforce_id,campaign_member_id)
           VALUES ('members',1,'mt-adams','create_org_lead',?,'added','00QTEST','00vTEST')""",
        (proposed,))
    conn.commit()
    return conn, followups.candidates(conn)[0]


def _reader(monkeypatch: Any, *, responded: bool = False,
            last_activity: str | None = None, tasks: list[dict[str, object]] | None = None,
            events: list[dict[str, object]] | None = None, fail: bool = False) -> None:
    """Install a deterministic GET-query result router."""
    def fake(query: str) -> tuple[list[dict[str, object]], str]:
        if fail:
            raise RuntimeError("reader unavailable")
        if "FROM CampaignMember" in query:
            return ([{"Id": "00vTEST", "HasResponded": responded}], "https://sf")
        if "FROM Lead" in query:
            return ([{"Id": "00QTEST", "LastActivityDate": last_activity}], "https://sf")
        if "FROM Task" in query:
            return (tasks or [], "https://sf")
        if "FROM Event" in query:
            return (events or [], "https://sf")
        raise AssertionError(query)
    monkeypatch.setattr(followups, "readonly_soql", fake)


def test_provenance_excludes_external_and_already_present(tmp_path: Path) -> None:
    """Salesforce-discovered or preexisting members can never enter the monitor."""
    conn, _ = _eligible(tmp_path)
    conn.execute("UPDATE crm_action_items SET state='already_present'")
    conn.commit()
    assert followups.candidates(conn) == []


def test_business_day_delay_crosses_weekend(tmp_path: Path) -> None:
    """Three business days from Friday lands Wednesday at the same instant."""
    _, candidate = _eligible(tmp_path)
    assert candidate.due_at.isoformat() == "2026-07-15T17:00:00+00:00"


def test_activity_signals_suppress(monkeypatch: Any, tmp_path: Path) -> None:
    """Completed post-enrollment Task is hard evidence and suppresses Slack."""
    _, candidate = _eligible(tmp_path)
    _reader(monkeypatch, tasks=[{"Id": "00T1", "IsClosed": True,
                                "CompletedDateTime": "2026-07-11T17:00:00Z"}])
    result = followups.inspect_activity(candidate, datetime(2026, 7, 16, tzinfo=timezone.utc))
    assert (result.status, result.evidence_kind) == ("activity", "task")


def test_open_task_future_event_and_old_task_do_not_suppress(monkeypatch: Any,
                                                              tmp_path: Path) -> None:
    """Only completed/past activity after membership counts."""
    _, candidate = _eligible(tmp_path)
    _reader(monkeypatch,
            tasks=[{"Id": "old", "IsClosed": True, "ActivityDate": "2026-07-09"},
                   {"Id": "open", "IsClosed": False, "ActivityDate": "2026-07-12"}],
            events=[{"Id": "future", "EndDateTime": "2026-07-20T00:00:00Z"}])
    assert followups.inspect_activity(
        candidate, datetime(2026, 7, 16, tzinfo=timezone.utc)).status == "none"


def test_last_activity_and_response_conservatively_suppress(monkeypatch: Any,
                                                             tmp_path: Path) -> None:
    """Response and date-only activity suppress nagging without claiming contact."""
    _, candidate = _eligible(tmp_path)
    _reader(monkeypatch, last_activity="2026-07-10")
    assert followups.inspect_activity(
        candidate, datetime(2026, 7, 16, tzinfo=timezone.utc)).evidence_kind == "activity_date_only"
    _reader(monkeypatch, responded=True)
    assert followups.inspect_activity(
        candidate, datetime(2026, 7, 16, tzinfo=timezone.utc)).evidence_kind == "campaign_response"


def test_reader_failure_is_unknown_and_posts_nothing(monkeypatch: Any, tmp_path: Path) -> None:
    """Any Salesforce read failure records unknown and fails closed."""
    conn, _ = _eligible(tmp_path)
    _reader(monkeypatch, fail=True)
    slack = FakeSlack()
    outcome = followups.run(slack, "C", conn, smoke=True)
    assert outcome.startswith("skip:") and slack.calls == []
    assert conn.execute("SELECT state FROM salesforce_followup_state").fetchone()[0] == "unknown"


def test_dry_run_has_zero_writes_and_clean_copy(monkeypatch: Any, tmp_path: Path) -> None:
    """Dry-run reads live truth but neither persists nor posts."""
    conn, candidate = _eligible(tmp_path)
    _reader(monkeypatch)
    before = conn.total_changes
    outcome = followups.run(None, "C", conn, dry_run=True, smoke=True)
    assert outcome.startswith("[dry-run] would post:")
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM salesforce_followup_state").fetchone()[0] == 0
    text = followups.build_message(candidate, smoke=True)
    assert "MT ADAMS" not in text and ":" in text and not any(c in text for c in "🚀🥇🔔")


def test_delivery_is_one_shot_and_timeout_never_retries(monkeypatch: Any,
                                                         tmp_path: Path) -> None:
    """Delivered and ambiguous Slack attempts both permanently consume the key."""
    conn, _ = _eligible(tmp_path)
    _reader(monkeypatch)
    slack = FakeSlack(fail=True)
    assert followups.run(slack, "C", conn, smoke=True).startswith("unknown:")
    assert followups.run(slack, "C", conn, smoke=True).startswith("skip:")
    assert len(slack.calls) == 1
