"""Fixture-only tests for exact completed-call evidence and local persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from grant_watch import db
from grant_watch.enrich import salesforce, salesforce_activity

NOW = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)
ACCOUNT = "001000000000001AAA"
PERSON = "003000000000001AAA"
TASK = "00T000000000001AAA"
OWNER = "005000000000001AAA"


def _task(**changes: object) -> dict[str, object]:
    """Build one exact Salesforce Task fixture."""
    record: dict[str, object] = {
        "Id": TASK,
        "TaskSubtype": "Call",
        "Status": "Completed",
        "IsClosed": True,
        "CompletedDateTime": (NOW - timedelta(days=7)).isoformat(),
        "AccountId": ACCOUNT,
        "WhoId": PERSON,
        "Owner": {
            "Id": OWNER,
            "Name": "Anthony Dambrosio",
            "Email": "anthony@monarchconnected.com",
        },
    }
    record.update(changes)
    return record


def test_exact_completed_call_resolves_owner_by_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact call returns typed evidence and an approved roster identity."""
    monkeypatch.setattr(
        salesforce,
        "readonly_soql",
        lambda _query: ([_task()], "https://sf.test"),
    )
    result = salesforce_activity.lookup_recent_completed_call(
        ACCOUNT, frozenset({PERSON}), now=NOW
    )
    assert result.status is salesforce_activity.ActivityStatus.VERIFIED_CALL
    assert result.owner_user_id == OWNER
    assert result.owner_slack_id == "U01DFJWQQJ3"
    assert result.person_id == PERSON
    assert result.completed_at == NOW - timedelta(days=7)


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"TaskSubtype": "Email"}, "generic activity is not a call"),
        ({"IsClosed": False}, "an open Task is not completed"),
        ({"AccountId": "001000000000009AAA"}, "wrong Account"),
        ({"WhoId": "003000000000009AAA"}, "wrong Contact/Lead"),
        ({"CompletedDateTime": "2026-06-01T00:00:00+00:00"}, "stale call"),
        ({"CompletedDateTime": "2026-07-22"}, "date is not a completion timestamp"),
    ],
)
def test_non_proving_tasks_are_not_calls(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, object], why: str
) -> None:
    """Every missing evidence condition fails closed as no recent call."""
    monkeypatch.setattr(
        salesforce,
        "readonly_soql",
        lambda _query: ([_task(**changes)], "https://sf.test"),
    )
    result = salesforce_activity.lookup_recent_completed_call(
        ACCOUNT, frozenset({PERSON}), now=NOW
    )
    assert result.status is salesforce_activity.ActivityStatus.NO_RECENT_CALL, why


def test_reader_outage_is_unavailable_not_no_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Salesforce read cannot establish absence of activity."""

    def unavailable(_query: str) -> tuple[list[dict[str, object]], str]:
        raise requests.Timeout("offline")

    monkeypatch.setattr(salesforce, "readonly_soql", unavailable)
    result = salesforce_activity.lookup_recent_completed_call(
        ACCOUNT, frozenset({PERSON}), now=NOW
    )
    assert result.status is salesforce_activity.ActivityStatus.UNAVAILABLE
    assert "Timeout" in result.error


def test_activity_snapshot_persists_exact_identity(tmp_path: Path) -> None:
    """The append-only local snapshot retains exact Task/User/roster identity."""
    conn = db.connect(tmp_path / "activity.db")
    conn.execute(
        "INSERT INTO leads(id,source,source_item_id,entity_name,status) "
        "VALUES (1,'fixture','1','Fixture District','new')"
    )
    evidence = salesforce_activity.ActivityEvidence(
        status=salesforce_activity.ActivityStatus.VERIFIED_CALL,
        checked_at=NOW,
        activity_id=TASK,
        activity_type="Call",
        completed_at=NOW - timedelta(days=7),
        account_id=ACCOUNT,
        person_id=PERSON,
        owner_user_id=OWNER,
        owner_name="Anthony Dambrosio",
        owner_email="anthony@monarchconnected.com",
        owner_slack_id="U01DFJWQQJ3",
        record_link=f"https://sf.test/lightning/r/Task/{TASK}/view",
    )
    snapshot_id = salesforce_activity.persist(conn, 1, evidence)
    row = conn.execute(
        "SELECT * FROM salesforce_activity_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    assert row["status"] == "verified_call"
    assert row["owner_user_id"] == OWNER
    assert row["owner_slack_id"] == "U01DFJWQQJ3"
    assert row["roster_status"] == "exact"
