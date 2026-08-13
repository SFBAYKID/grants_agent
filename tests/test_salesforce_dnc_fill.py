"""Do-not-call propagation for existing Salesforce Leads is fail-closed."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from grant_watch import db, salesforce_lead_fill
from grant_watch.enrich import salesforce_campaign_gateway as gateway_module
from grant_watch.enrich.salesforce_campaign_gateway import CreateResult
from tests.contact_support import verified_contact_evidence

LEAD_ID = 41
SALESFORCE_ID = "00Q2M000019GMBvUAO"


class _Gateway:
    """Sequenced gateway double that records compliance and fill call ordering."""

    def __init__(
        self,
        *,
        markers: Iterator[CreateResult | BaseException] | None = None,
        fills: Iterator[CreateResult | BaseException] | None = None,
    ) -> None:
        """Configure ordered marker and fill outcomes for one workflow test."""
        self.markers = markers or iter(
            [CreateResult(True, SALESFORCE_ID, error="marked do-not-call")]
        )
        self.fills = fills or iter(
            [CreateResult(True, SALESFORCE_ID, error="filled Title")]
        )
        self.events: list[str] = []

    def mark_lead_do_not_call(self, record_id: str) -> CreateResult:
        """Return or raise the next configured marker result."""
        assert record_id == SALESFORCE_ID
        self.events.append("mark")
        outcome = next(self.markers)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def fill_lead_blanks(
        self, record_id: str, fields: dict[str, object]
    ) -> CreateResult:
        """Return or raise the next configured fill result."""
        assert record_id == SALESFORCE_ID
        assert fields
        self.events.append("fill")
        outcome = next(self.fills)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _run_plan(
    monkeypatch: pytest.MonkeyPatch,
    plan: salesforce_lead_fill.LeadFillPlan,
    gateway: _Gateway,
    *,
    dry_run: bool = False,
) -> salesforce_lead_fill.FillOutcome:
    """Run one test-owned linked Lead with a fixed typed fill plan."""
    monkeypatch.setattr(
        salesforce_lead_fill,
        "linked_leads",
        lambda _conn, _limit: [{"lead_id": LEAD_ID, "salesforce_id": SALESFORCE_ID}],
    )
    monkeypatch.setattr(
        salesforce_lead_fill,
        "build_fill_plan",
        lambda _conn, _lead_id: plan,
    )
    conn = sqlite3.connect(":memory:")
    try:
        return salesforce_lead_fill.run(conn, gateway, limit=1, dry_run=dry_run)
    finally:
        conn.close()


def test_dnc_marker_precedes_every_blank_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No contact or phone field reaches Salesforce before its warning is durable."""
    gateway = _Gateway()
    outcome = _run_plan(
        monkeypatch,
        salesforce_lead_fill.LeadFillPlan({"Title": "IT Director"}, True),
        gateway,
    )
    assert gateway.events == ["mark", "fill"]
    assert outcome.filled == 1 and outcome.failed == 0


def test_dnc_only_plan_still_executes_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ordinary blank field is required to propagate the compliance state."""
    gateway = _Gateway()
    outcome = _run_plan(
        monkeypatch, salesforce_lead_fill.LeadFillPlan({}, True), gateway
    )
    assert gateway.events == ["mark"]
    assert outcome.filled == 1


@pytest.mark.parametrize(
    "marker",
    [
        CreateResult(False, SALESFORCE_ID, error="HTTP 500"),
        RuntimeError("write outcome unknown"),
    ],
)
def test_marker_failure_or_ambiguity_blocks_every_fill(
    monkeypatch: pytest.MonkeyPatch,
    marker: CreateResult | BaseException,
) -> None:
    """An absent or uncertain warning makes later contact writes unsafe."""
    gateway = _Gateway(markers=iter([marker]))
    outcome = _run_plan(
        monkeypatch,
        salesforce_lead_fill.LeadFillPlan(
            {"Phone": "555-0100", "Title": "IT Director"}, True
        ),
        gateway,
    )
    assert gateway.events == ["mark"]
    assert outcome.failed == 1 and outcome.filled == 0


def test_already_marked_is_success_and_fill_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotent compliance result cannot wedge a later retry."""
    gateway = _Gateway(
        markers=iter([CreateResult(True, SALESFORCE_ID, error="already marked")])
    )
    outcome = _run_plan(
        monkeypatch,
        salesforce_lead_fill.LeadFillPlan({"Title": "IT Director"}, True),
        gateway,
    )
    assert gateway.events == ["mark", "fill"]
    assert outcome.filled == 1


def test_marker_success_then_fill_failure_is_safely_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safe partial state retains one warning and retries only blank fields."""
    gateway = _Gateway(
        markers=iter(
            [
                CreateResult(True, SALESFORCE_ID, error="marked do-not-call"),
                CreateResult(True, SALESFORCE_ID, error="already marked"),
            ]
        ),
        fills=iter(
            [
                CreateResult(False, SALESFORCE_ID, error="HTTP 503"),
                CreateResult(True, SALESFORCE_ID, error="filled Title"),
            ]
        ),
    )
    plan = salesforce_lead_fill.LeadFillPlan({"Title": "IT Director"}, True)

    first = _run_plan(monkeypatch, plan, gateway)
    second = _run_plan(monkeypatch, plan, gateway)

    assert first.failed == 1 and second.filled == 1
    assert gateway.events == ["mark", "fill", "mark", "fill"]


def test_non_dnc_contact_never_calls_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean selected contact cannot inherit another person's compliance flag."""
    gateway = _Gateway()
    outcome = _run_plan(
        monkeypatch,
        salesforce_lead_fill.LeadFillPlan({"Title": "IT Director"}, False),
        gateway,
    )
    assert gateway.events == ["fill"]
    assert outcome.filled == 1


def test_only_the_selected_best_contact_controls_dnc(tmp_path: Path) -> None:
    """A lower-ranked flagged vendor row cannot mark the verified person's Lead."""
    conn = db.connect(tmp_path / "leads.db")
    lead_id = int(
        conn.execute(
            """INSERT INTO leads
                 (source,source_item_id,entity_name,state,detail_url,lead_grade)
               VALUES ('test','1','Example District','CA','https://example.test','gold')"""
        ).lastrowid
        or 0
    )
    db.save_vendor_contact(
        conn,
        lead_id,
        "Vendor Person",
        "CIO",
        "vendor@example.test",
        "",
        "vendor-1",
        do_not_call=True,
    )
    source = "https://example.test/staff"
    db.save_contact(
        conn,
        lead_id,
        "Verified Person",
        "IT Director",
        "verified@example.test",
        "",
        source,
        "high",
        field_evidence=verified_contact_evidence(
            "Verified Person", "verified@example.test", source, title="IT Director"
        ),
    )

    plan = salesforce_lead_fill.build_fill_plan(conn, lead_id)

    assert plan.do_not_call is False
    assert plan.fields["Email"] == "verified@example.test"
    assert plan.fields["Title"] == "IT Director"
    conn.close()


def test_dry_run_previews_marker_without_salesforce_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator sees the compliance write while preview remains write-free."""
    gateway = _Gateway()
    outcome = _run_plan(
        monkeypatch,
        salesforce_lead_fill.LeadFillPlan({}, True),
        gateway,
        dry_run=True,
    )
    assert "prepend fixed DO NOT CALL marker" in capsys.readouterr().out
    assert gateway.events == []
    assert outcome.filled == 0 and outcome.failed == 0


def test_foreign_org_marker_result_skips_fill_without_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Lead ID from the other Salesforce org remains a routine no-write skip."""
    gateway = _Gateway(
        markers=iter([CreateResult(False, SALESFORCE_ID, error="not in this org")])
    )
    outcome = _run_plan(
        monkeypatch,
        salesforce_lead_fill.LeadFillPlan({"Title": "IT Director"}, True),
        gateway,
    )
    assert gateway.events == ["mark"]
    assert outcome.failed == 0 and outcome.filled == 0


class _Response:
    """Minimal requests-shaped response for gateway safety tests."""

    def __init__(self, status: int, body: dict[str, object]) -> None:
        """Store one HTTP status and decoded response body."""
        self.status_code = status
        self._body = body
        self.text = "conflict" if status == 412 else ""

    def json(self) -> dict[str, object]:
        """Return the configured Salesforce JSON body."""
        return self._body


def test_write_scope_rejection_precedes_dnc_record_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong-org or sandbox credentials cannot even inspect the target Lead."""
    gateway = gateway_module.SalesforceCampaignGateway()
    calls: list[str] = []
    monkeypatch.setattr(
        gateway,
        "verify_write_scope",
        lambda: (_ for _ in ()).throw(PermissionError("wrong org")),
    )
    monkeypatch.setattr(
        gateway_module.requests,
        "get",
        lambda *_args, **_kwargs: calls.append("GET"),
    )
    monkeypatch.setattr(
        gateway_module.requests,
        "patch",
        lambda *_args, **_kwargs: calls.append("PATCH"),
    )

    with pytest.raises(PermissionError, match="wrong org"):
        gateway.mark_lead_do_not_call(SALESFORCE_ID)
    assert calls == []


def test_concurrent_description_edit_refuses_conditional_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A human edit between GET and PATCH yields 412 and is never overwritten."""
    gateway = gateway_module.SalesforceCampaignGateway()
    patch_headers: dict[str, str] = {}
    monkeypatch.setattr(gateway, "verify_write_scope", lambda: None)
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", "https://writer.test"))
    monkeypatch.setattr(
        gateway_module.requests,
        "get",
        lambda *_args, **_kwargs: _Response(
            200,
            {
                "Description": "Human-authored current text.",
                "LastModifiedDate": "2026-08-13T12:34:56.987+0000",
            },
        ),
    )

    def conflict(*_args: object, **kwargs: object) -> _Response:
        """Capture the precondition and simulate Salesforce rejecting stale state."""
        patch_headers.update(dict(kwargs.get("headers") or {}))
        return _Response(412, {})

    monkeypatch.setattr(gateway_module.requests, "patch", conflict)

    result = gateway.mark_lead_do_not_call(SALESFORCE_ID)

    assert result.success is False
    assert "refusing to overwrite" in result.error
    assert patch_headers["If-Unmodified-Since"] == ("Thu, 13 Aug 2026 12:34:56 GMT")


def test_concurrent_blank_fill_edit_refuses_conditional_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary blank-fill path also treats Salesforce 412 as a safe conflict."""
    gateway = gateway_module.SalesforceCampaignGateway()
    patch_headers: dict[str, str] = {}
    monkeypatch.setattr(gateway, "verify_write_scope", lambda: None)
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", "https://writer.test"))
    monkeypatch.setattr(
        gateway_module.requests,
        "get",
        lambda *_args, **_kwargs: _Response(
            200,
            {
                "Title": "",
                "LastModifiedDate": "2026-08-13T12:34:56.987+0000",
            },
        ),
    )

    def conflict(*_args: object, **kwargs: object) -> _Response:
        """Capture the version precondition and reject the stale blank snapshot."""
        patch_headers.update(dict(kwargs.get("headers") or {}))
        return _Response(412, {})

    monkeypatch.setattr(gateway_module.requests, "patch", conflict)

    result = gateway.fill_lead_blanks(SALESFORCE_ID, {"Title": "IT Director"})

    assert result.success is False
    assert "record changed during fill" in result.error
    assert patch_headers["If-Unmodified-Since"] == ("Thu, 13 Aug 2026 12:34:56 GMT")


@pytest.mark.parametrize("last_modified", [None, "not-a-timestamp"])
@pytest.mark.parametrize("operation", ["fill", "marker"])
def test_missing_or_malformed_last_modified_date_never_reaches_patch(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    last_modified: object,
) -> None:
    """Neither guarded update may degrade to an unconditional Salesforce write."""
    gateway = gateway_module.SalesforceCampaignGateway()
    patches: list[str] = []
    monkeypatch.setattr(gateway, "verify_write_scope", lambda: None)
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", "https://writer.test"))
    body: dict[str, object] = {
        "Title": "",
        "Description": "Human-authored text.",
    }
    if last_modified is not None:
        body["LastModifiedDate"] = last_modified
    monkeypatch.setattr(
        gateway_module.requests,
        "get",
        lambda *_args, **_kwargs: _Response(200, body),
    )
    monkeypatch.setattr(
        gateway_module.requests,
        "patch",
        lambda *_args, **_kwargs: patches.append("PATCH"),
    )

    result = (
        gateway.fill_lead_blanks(SALESFORCE_ID, {"Title": "IT Director"})
        if operation == "fill"
        else gateway.mark_lead_do_not_call(SALESFORCE_ID)
    )

    assert result.success is False
    assert "valid LastModifiedDate" in result.error
    assert patches == []
