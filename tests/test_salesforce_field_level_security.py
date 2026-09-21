"""A field this integration user cannot read must not become a lie about Salesforce.

Production, 2026-09-21: a rep asked Grant to put Oregon leads into a Salesforce
campaign. Every Campaign Type she named came back "not active", every campaign name
search came back a 400 that Grant described as a temporary hiccup, and the task was
abandoned. Neither statement was true. The integration user simply has no
field-level read on `Campaign.Type`, `Status`, `IsActive` and `OwnerId` -- the same
gap already documented for `Lead.DoNotCall` -- and the code turned that one
permission fact into two confident, wrong sentences.

These fixtures model Salesforce's REAL contract: describe OMITS a field the user
cannot read, and a SELECT naming one answers HTTP 400 INVALID_FIELD.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from grant_watch.enrich import salesforce_campaign_gateway as gw
from grant_watch.enrich import salesforce_rest

#: Fields this integration user cannot read, exactly as production behaves.
_HIDDEN = ("Type", "Status", "IsActive", "OwnerId", "Owner.Name")


class _Response:
    """requests.Response stand-in carrying a real status and a real body."""

    def __init__(self, status_code: int, payload: Any, text: str = "") -> None:
        """Record what this fake response will report."""
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self) -> Any:
        """Return the decoded body, or raise like requests on non-JSON."""
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _invalid_field(field: str) -> _Response:
    """Salesforce's actual answer when a SELECT names an unreadable field."""
    return _Response(
        400,
        [
            {
                "message": (
                    f"\nSELECT ... FROM Campaign\n       ^\nERROR at Row:1:Column:8\n"
                    f"No such column '{field}' on entity 'Campaign'."
                ),
                "errorCode": "INVALID_FIELD",
            }
        ],
    )


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> gw.SalesforceCampaignGateway:
    """A gateway with credentials stubbed out; only HTTP behaviour is under test."""
    monkeypatch.setattr(
        gw.SalesforceCampaignGateway,
        "_auth",
        lambda self, force=False: ("tok", "https://x.my.salesforce.com"),
    )
    return gw.SalesforceCampaignGateway()


def test_campaign_name_search_survives_a_least_privilege_user(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE production failure: searching by name must not 400 on unused fields.

    Mutation control: restore any of the four discarded fields to the SELECT and
    this fails, because the fake rejects them exactly as Salesforce does.
    """
    seen: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> _Response:
        """Reject any SELECT naming a field this user may not read."""
        soql = kwargs.get("params", {}).get("q", "")
        seen.append(soql)
        selected = soql.split(" FROM ")[0] if " FROM " in soql else soql
        for field in _HIDDEN:
            if field in selected:
                return _invalid_field(field)
        return _Response(
            200, {"records": [{"Id": "701iL000005wpSJQAY", "Name": "GRANTS"}]}
        )

    monkeypatch.setattr(gw.requests, "get", fake_get)

    found = gateway.search_campaigns("GRANTS")

    assert [record.name for record in found] == ["GRANTS"]
    assert found[0].record_id == "701iL000005wpSJQAY"
    # The precondition that makes this test meaningful: the fake WOULD have refused
    # the old query, so a pass here cannot be an accident of a permissive fixture.
    assert _invalid_field("Type").status_code == 400
    assert "Type" not in seen[0] and "IsActive" not in seen[0]


def test_an_unreadable_picklist_is_reported_as_unreadable_not_inactive(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Describe omits an invisible field; that is not evidence any value is inactive."""

    def fake_get(url: str, **kwargs: Any) -> _Response:
        """Return a describe payload with Type withheld by field-level security."""
        return _Response(
            200,
            {
                "fields": [
                    {"name": "Id"},
                    {"name": "Name"},
                    {
                        "name": "Status",
                        "picklistValues": [{"value": "Planned", "active": True}],
                    },
                ]
            },
        )

    monkeypatch.setattr(gw.requests, "get", fake_get)

    with pytest.raises(salesforce_rest.PicklistNotReadable) as excinfo:
        gateway.campaign_picklists()

    message = str(excinfo.value)
    assert "Type" in message
    assert "field-level security" in message
    # It must NOT claim anything about which values are active.
    assert "not active" not in message


def test_a_readable_picklist_still_returns_its_active_values(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the ordinary path is unchanged, and inactive values stay excluded."""

    def fake_get(url: str, **kwargs: Any) -> _Response:
        """Return a describe payload exposing both fields."""
        return _Response(
            200,
            {
                "fields": [
                    {
                        "name": "Type",
                        "picklistValues": [
                            {"value": "Other", "active": True},
                            {"value": "Retired", "active": False},
                        ],
                    },
                    {
                        "name": "Status",
                        "picklistValues": [{"value": "Planned", "active": True}],
                    },
                ]
            },
        )

    monkeypatch.setattr(gw.requests, "get", fake_get)

    types, statuses = gateway.campaign_picklists()

    assert types == {"Other"}
    assert statuses == {"Planned"}


def test_a_refusal_names_the_values_that_would_work() -> None:
    """A rep must never have to guess a picklist value Grant is already holding."""
    message = salesforce_rest.picklist_refusal(
        "Type", "Event", {"Webinar", "Conference"}
    )

    assert "'Event' is not active" in message
    assert "Conference, Webinar" in message


def test_a_refusal_with_nothing_active_says_so_rather_than_listing_nothing() -> None:
    """An empty active set is a real state and must read as one."""
    assert "(none are active)" in salesforce_rest.picklist_refusal(
        "Type", "Event", set()
    )


def test_salesforce_explains_its_own_400_instead_of_a_bare_status_line(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error a rep sees must carry Salesforce's reason and rule out a retry."""
    monkeypatch.setattr(
        gw.requests, "get", lambda url, **kwargs: _invalid_field("Type")
    )

    with pytest.raises(requests.HTTPError) as excinfo:
        gateway._get("query", {"q": "SELECT Type FROM Campaign"})

    message = str(excinfo.value)
    assert "INVALID_FIELD" in message
    assert "No such column 'Type'" in message
    assert "retrying will not help" in message
    # Still an HTTPError, so every existing `except requests.RequestException` holds.
    assert isinstance(excinfo.value, requests.RequestException)


def test_a_genuinely_transient_failure_is_not_labelled_permanent(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: a 503 must not tell a rep that retrying cannot help."""
    monkeypatch.setattr(
        gw.requests,
        "get",
        lambda url, **kwargs: _Response(
            503, [{"message": "Server unavailable", "errorCode": "SERVER_UNAVAILABLE"}]
        ),
    )

    with pytest.raises(requests.HTTPError) as excinfo:
        gateway._get("query", {"q": "SELECT Id FROM Campaign"})

    assert "retrying will not help" not in str(excinfo.value)
    assert "SERVER_UNAVAILABLE" in str(excinfo.value)


def test_a_non_json_error_body_falls_back_to_its_own_text(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ugly true string beats a tidy invented one."""
    monkeypatch.setattr(
        gw.requests,
        "get",
        lambda url, **kwargs: _Response(502, None, text="<html>Bad Gateway</html>"),
    )

    with pytest.raises(requests.HTTPError) as excinfo:
        gateway._get("query", {"q": "SELECT Id FROM Campaign"})

    assert "Bad Gateway" in str(excinfo.value)


def test_a_permission_failure_forbids_the_retry_advice_that_looped_a_rep() -> None:
    """The judgement travels with the error, because the response is gone by then."""
    response = _invalid_field("Type")
    error = requests.HTTPError("HTTP 400 from Salesforce", response=response)

    guidance = salesforce_rest.permanent_failure_guidance(error)

    assert "Do NOT suggest retrying" in guidance
    assert "admin change" in guidance


def test_a_transient_failure_gets_no_permanence_guidance() -> None:
    """Control: a real outage must still be describable as one."""
    response = _Response(503, [{"message": "down", "errorCode": "SERVER_UNAVAILABLE"}])
    error = requests.HTTPError("HTTP 503", response=response)

    assert salesforce_rest.permanent_failure_guidance(error) == ""
    assert (
        salesforce_rest.permanent_failure_guidance(ValueError("something else")) == ""
    )


def test_an_unreadable_picklist_forbids_asking_the_rep_to_guess() -> None:
    """The exact loop: Grant asked a rep which Types her org had enabled."""
    guidance = salesforce_rest.permanent_failure_guidance(
        salesforce_rest.PicklistNotReadable("Type withheld")
    )

    assert "do NOT ask the rep which values" in guidance.replace("Do NOT", "do NOT")
    assert "admin" in guidance


def test_the_campaign_search_tool_hands_the_model_the_permanence_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end at the tool boundary: the rep-facing path carries the guidance."""
    from grant_watch.slack import tools

    monkeypatch.setattr(
        gw.SalesforceCampaignGateway,
        "_auth",
        lambda self, force=False: ("tok", "https://x.my.salesforce.com"),
    )
    monkeypatch.setattr(
        gw.requests, "get", lambda url, **kwargs: _invalid_field("Type")
    )

    result = tools.salesforce_campaign_search("GRANTS")

    assert result.startswith("ERROR: Campaign search failed")
    assert "INVALID_FIELD" in result, "the rep-facing path must carry the real cause"
    assert "Do NOT suggest retrying" in result
    # The guidance is for the model only and must never be shown verbatim.
    from grant_watch.presentation import for_human

    assert "Do NOT suggest retrying" not in for_human(result)
