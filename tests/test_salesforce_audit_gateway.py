"""HTTP-contract tests for singular Salesforce Lead audit artifacts."""

from __future__ import annotations

import base64
import inspect
from typing import Any

import pytest

from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod

LEAD_ID = "00Q000000000001"
ACTION_ID = "11111111-2222-3333-4444-555555555555"


class Response:
    """Minimal deterministic Salesforce composite response."""

    status_code = 200
    text = ""

    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self.responses = responses or [
            {"referenceId": "grantResearchNote", "httpStatusCode": 201,
             "body": {"id": "069000000000001"}},
            {"referenceId": "grantResearchLink", "httpStatusCode": 201,
             "body": {"id": "06A000000000001"}},
            {"referenceId": "grantAuditTask", "httpStatusCode": 201,
             "body": {"id": "00T000000000001"}},
        ]

    def json(self) -> dict[str, object]:
        """Return exact IDs for the three fixed subrequests."""
        return {"compositeResponse": self.responses}


def test_audit_bundle_is_one_fixed_all_or_none_transaction(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """One audit action cannot target a second Lead or arbitrary Salesforce object."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", "https://writer.test"))
    monkeypatch.setattr(
        gateway, "lead_audit_snapshot", lambda *_args: gateway_mod.LeadAuditSnapshot())
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> Response:
        """Capture the one exact outbound transaction."""
        calls.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(gateway_mod.requests, "post", fake_post)
    result = gateway.create_lead_audit_bundle(
        LEAD_ID, ACTION_ID, "Verified sources", "No customer outreach. Action " + ACTION_ID,
        "2026-07-15")

    assert result.success and len(calls) == 1
    assert calls[0]["url"] == "https://writer.test/services/data/v60.0/composite"
    body = calls[0]["json"]
    assert body["allOrNone"] is True
    requests = body["compositeRequest"]
    assert [item["referenceId"] for item in requests] == [
        "grantResearchNote", "grantResearchLink", "grantAuditTask"]
    assert all(item["method"] == "POST" for item in requests)
    assert requests[1]["body"]["LinkedEntityId"] == LEAD_ID
    assert requests[2]["body"]["WhoId"] == LEAD_ID
    assert not any("Lead/" in item["url"] or "Campaign" in item["url"]
                   or "Opportunity" in item["url"] for item in requests)


def test_existing_lead_enrichment_is_one_four_part_transaction(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Lead fields and all audit artifacts share one all-or-none HTTP request."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", "https://writer.test"))
    monkeypatch.setattr(
        gateway, "lead_audit_snapshot", lambda *_args: gateway_mod.LeadAuditSnapshot())
    monkeypatch.setattr(
        gateway, "lead_enrichment_snapshot",
        lambda _lead_id: gateway_mod.LeadEnrichmentSnapshot(
            LEAD_ID, "Test District", "person@test.example", "stamp", {}, "link"))
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> Response:
        calls.append({"url": url, **kwargs})
        return Response([
            {"referenceId": "grantLeadUpdate", "httpStatusCode": 204, "body": None},
            {"referenceId": "grantResearchNote", "httpStatusCode": 201,
             "body": {"id": "069000000000001"}},
            {"referenceId": "grantResearchLink", "httpStatusCode": 201,
             "body": {"id": "06A000000000001"}},
            {"referenceId": "grantAuditTask", "httpStatusCode": 201,
             "body": {"id": "00T000000000001"}},
        ])

    monkeypatch.setattr(gateway_mod.requests, "post", fake_post)
    result = gateway.enrich_lead_with_audit_bundle(
        LEAD_ID, {"Website": "https://district.test"}, "stamp", ACTION_ID,
        "Verified sources", "No customer outreach. Action " + ACTION_ID, "2026-07-15")

    assert result.success and len(calls) == 1 and result.lead_id == LEAD_ID
    body = calls[0]["json"]
    assert body["allOrNone"] is True
    items = body["compositeRequest"]
    assert [item["referenceId"] for item in items] == [
        "grantLeadUpdate", "grantResearchNote", "grantResearchLink", "grantAuditTask"]
    assert items[0] == {
        "method": "PATCH", "url": f"/services/data/v60.0/sobjects/Lead/{LEAD_ID}",
        "referenceId": "grantLeadUpdate", "body": {"Website": "https://district.test"}}
    assert items[2]["body"]["LinkedEntityId"] == LEAD_ID
    assert items[3]["body"]["WhoId"] == LEAD_ID


def test_person_lead_and_audit_are_one_four_part_transaction(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Standalone person creation cannot commit separately from its audit records."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", "https://writer.test"))
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> Response:
        calls.append({"url": url, **kwargs})
        return Response([
            {"referenceId": "grantPersonLead", "httpStatusCode": 201,
             "body": {"id": LEAD_ID}},
            {"referenceId": "grantResearchNote", "httpStatusCode": 201,
             "body": {"id": "069000000000001"}},
            {"referenceId": "grantResearchLink", "httpStatusCode": 201,
             "body": {"id": "06A000000000001"}},
            {"referenceId": "grantAuditTask", "httpStatusCode": 201,
             "body": {"id": "00T000000000001"}},
        ])

    monkeypatch.setattr(gateway_mod.requests, "post", fake_post)
    result = gateway.create_person_lead_with_audit_bundle(
        {"Company": "Test District", "LastName": "Person",
         "Email": "person@test.example", "Description": f"Action {ACTION_ID}"},
        ACTION_ID, "Verified sources", "No customer outreach. Action " + ACTION_ID,
        "2026-07-15")

    assert result.success and len(calls) == 1 and result.lead_id == LEAD_ID
    items = calls[0]["json"]["compositeRequest"]
    assert [item["referenceId"] for item in items] == [
        "grantPersonLead", "grantResearchNote", "grantResearchLink", "grantAuditTask"]
    assert items[2]["body"]["LinkedEntityId"] == "@{grantPersonLead.id}"
    assert items[3]["body"]["WhoId"] == "@{grantPersonLead.id}"


def test_composite_rollback_preserves_exact_subrequest_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A received rollback retains the failing reference and Salesforce error body."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", "https://writer.test"))
    monkeypatch.setattr(
        gateway, "lead_audit_snapshot", lambda *_args: gateway_mod.LeadAuditSnapshot())
    monkeypatch.setattr(
        gateway, "lead_enrichment_snapshot",
        lambda _lead_id: gateway_mod.LeadEnrichmentSnapshot(
            LEAD_ID, "Test District", "person@test.example", "stamp", {}, "link"))

    def fake_post(_url: str, **_kwargs: Any) -> Response:
        return Response([
            {"referenceId": "grantLeadUpdate", "httpStatusCode": 400,
             "body": [{"errorCode": "PROCESSING_HALTED"}]},
            {"referenceId": "grantResearchNote", "httpStatusCode": 412,
             "body": [{"errorCode": "PROCESSING_HALTED"}]},
            {"referenceId": "grantResearchLink", "httpStatusCode": 412,
             "body": [{"errorCode": "PROCESSING_HALTED"}]},
            {"referenceId": "grantAuditTask", "httpStatusCode": 400,
             "body": [{"errorCode": "INVALID_FIELD", "message": "bad Status"}]},
        ])

    monkeypatch.setattr(gateway_mod.requests, "post", fake_post)
    result = gateway.enrich_lead_with_audit_bundle(
        LEAD_ID, {"Website": "https://district.test"}, "stamp", ACTION_ID,
        "Verified sources", "No customer outreach. Action " + ACTION_ID, "2026-07-15")
    assert result.success is False
    assert result.error.startswith("grantAuditTask HTTP 400")
    assert "INVALID_FIELD" in result.error and "grantLeadUpdate HTTP 400" in result.error


def test_audit_readback_compares_note_link_task_and_truthful_copy(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Completion requires exact Enhanced Note content, link, and Task readback."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    note_body = "Verified sources"
    task_body = "Grant updated Website. No customer outreach was performed. Action " + ACTION_ID

    def fake_get(path: str, _params: dict[str, str]) -> dict[str, object]:
        if path.startswith("sobjects/ContentNote/"):
            return {"Title": f"Grant research — {ACTION_ID}",
                    "Content": base64.b64encode(note_body.encode()).decode()}
        if path.startswith("sobjects/ContentDocumentLink/"):
            return {"ContentDocumentId": "069000000000001", "LinkedEntityId": LEAD_ID,
                    "ShareType": "V", "Visibility": "InternalUsers"}
        if path.startswith("sobjects/Task/"):
            return {"WhoId": LEAD_ID, "Subject": "Grant system: CRM research updated",
                    "Status": "Completed", "Description": task_body}
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_get", fake_get)
    result = gateway_mod.LeadAuditResult(
        True, "069000000000001", "06A000000000001", "00T000000000001")
    assert gateway.verify_lead_audit_bundle(
        LEAD_ID, ACTION_ID, note_body, task_body, result)


def test_partial_audit_never_blindly_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial prior result fails closed before any Salesforce POST."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(
        gateway, "lead_audit_snapshot",
        lambda *_args: gateway_mod.LeadAuditSnapshot(note_id="069000000000001"))
    called = False

    def fake_post(*_args: object, **_kwargs: object) -> Response:
        nonlocal called
        called = True
        return Response()

    monkeypatch.setattr(gateway_mod.requests, "post", fake_post)
    with pytest.raises(ValueError, match="partial"):
        gateway.create_lead_audit_bundle(
            LEAD_ID, ACTION_ID, "Verified sources", "Action " + ACTION_ID, "2026-07-15")
    assert called is False


def test_audit_snapshot_filters_task_description_in_memory(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Task.Description is selected but never used as an unsupported SOQL filter."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    queries: list[str] = []

    def fake_get(_path: str, params: dict[str, str]) -> dict[str, object]:
        query = params["q"]
        queries.append(query)
        if "FROM ContentDocumentLink" in query:
            return {"records": []}
        if "FROM Task" in query:
            return {"records": [
                {"Id": "00T000000000009", "Description": "Action another-id."},
                {"Id": "00T000000000001", "Description": f"Action {ACTION_ID}."},
            ]}
        raise AssertionError(query)

    monkeypatch.setattr(gateway, "_get", fake_get)
    snapshot = gateway.lead_audit_snapshot(LEAD_ID, ACTION_ID)
    assert snapshot.task_id == "00T000000000001"
    task_query = next(item for item in queries if "FROM Task" in item)
    assert "Description LIKE" not in task_query
    assert "SELECT Id,Description" in task_query


def test_salesforce_gateway_exposes_no_delete_or_put_http_method() -> None:
    """The Salesforce writer module has no DELETE or PUT request primitive."""
    source = inspect.getsource(gateway_mod)
    assert "requests.delete" not in source
    assert "requests.put" not in source
    assert '"method": "DELETE"' not in source
    assert '"method": "PUT"' not in source
