"""A field this integration user cannot read must not become a lie about Salesforce.

Production, 2026-09-21: a rep asked Grant to put Oregon leads into a Salesforce
campaign. Every Campaign Type she named came back "not active", every campaign name
search came back HTTP 400 that Grant described as "a temporary hiccup with their
API", and the task was abandoned. Neither statement was true. The integration user
simply has no field-level read on `Campaign.Type`, `Status`, `IsActive` and
`OwnerId` -- the same gap already documented for `Lead.DoNotCall` -- and the code
turned one permission fact into two confident, wrong sentences.

These fixtures model Salesforce's REAL contract, which is what the repo's previous
fixture failures did not:

- describe OMITS a field the user cannot read, rather than returning it empty;
- a hidden field answers 400 INVALID_FIELD wherever it appears, SELECT or WHERE;
- the error body is ~400 characters of echoed SOQL before the actual sentence, so
  anything appended after it is truncated away before a rep sees it;
- a create reports per-record failures under `statusCode`, not `errorCode`.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from grant_watch.enrich import salesforce_campaign_gateway as gw
from grant_watch.enrich import salesforce_rest
from grant_watch.presentation import for_human

#: Fields this integration user cannot read, exactly as production behaves.
_HIDDEN = ("Type", "Status", "IsActive", "OwnerId", "Owner.Name")

#: The OLD query, kept so the fixture's precondition can be asserted against the
#: thing it is supposed to reject rather than against a constant.
_OLD_SOQL = (
    "SELECT Id,Name,Status,Type,IsActive,Owner.Name FROM Campaign "
    "WHERE Name LIKE '%GRANTS%' ORDER BY LastModifiedDate DESC LIMIT 20"
)


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


def _invalid_field(field: str, soql: str = _OLD_SOQL) -> _Response:
    """Salesforce's ACTUAL answer, at production length.

    A real INVALID_FIELD body echoes the whole failing query, a caret, a row/column
    marker and a closing advice paragraph. The short fixture this file first used
    was ~90 characters, which hid the fact that every rep-facing caller truncates.
    """
    return _Response(
        400,
        [
            {
                "message": (
                    f"\n{soql}\n{' ' * 30}^\nERROR at Row:1:Column:53\n"
                    f"No such column '{field}' on entity 'Campaign'. If you are "
                    "attempting to use a custom field, be sure to append the '__c' "
                    "after the custom field name. Please reference your WSDL or the "
                    "describe call for the appropriate names."
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
    monkeypatch.setattr(
        gw.SalesforceCampaignGateway, "verify_write_scope", lambda self: None
    )
    return gw.SalesforceCampaignGateway()


def _hidden_field_in(soql: str) -> str:
    """Return the first unreadable field named ANYWHERE in the query.

    Salesforce rejects a hidden field in a WHERE or an ORDER BY exactly as it
    rejects one in a SELECT. A fake that inspects only the SELECT clause lets three
    of the five hidden names through, which is how a WHERE-clause regression would
    have passed this whole file.
    """
    return next((field for field in _HIDDEN if field in soql), "")


def test_campaign_name_search_survives_a_least_privilege_user(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE production failure: searching by name must not 400 on unused fields."""
    seen: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> _Response:
        """Reject any query naming a field this user may not read, anywhere."""
        soql = kwargs.get("params", {}).get("q", "")
        seen.append(soql)
        hidden = _hidden_field_in(soql)
        if hidden:
            return _invalid_field(hidden, soql)
        return _Response(
            200, {"records": [{"Id": "701iL000005wpSJQAY", "Name": "GRANTS"}]}
        )

    monkeypatch.setattr(gw.requests, "get", fake_get)

    # The precondition that makes this test meaningful, asserted against the OLD
    # query rather than against a constant the helper was handed.
    assert fake_get("", params={"q": _OLD_SOQL}).status_code == 400

    found = gateway.search_campaigns("GRANTS")

    assert [record.name for record in found] == ["GRANTS"]
    assert found[0].record_id == "701iL000005wpSJQAY"
    assert not _hidden_field_in(seen[-1]), f"query still names a hidden field: {seen}"


def test_an_unreadable_picklist_is_reported_as_unreadable_not_inactive(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Describe omits an invisible field; that is not evidence any value is inactive."""
    monkeypatch.setattr(
        gw.requests,
        "get",
        lambda url, **kwargs: _Response(
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
        ),
    )

    with pytest.raises(salesforce_rest.PicklistNotReadable) as excinfo:
        gateway.campaign_picklists()

    message = str(excinfo.value)
    assert "Type" in message
    assert "field-level security" in message
    assert "not active" not in message, "it must not claim anything about values"


def test_a_readable_picklist_still_returns_its_active_values(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the ordinary path is unchanged, and inactive values stay excluded."""
    monkeypatch.setattr(
        gw.requests,
        "get",
        lambda url, **kwargs: _Response(
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
        ),
    )

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
    assert "Retrying will not help" in message
    assert isinstance(excinfo.value, requests.RequestException)


def test_the_cause_survives_every_truncation_a_rep_facing_caller_applies(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE reason the permanence sentence goes first, at production body length.

    Measured: the real message is ~500 characters. Appended last, the permanence
    sentence was cut at every truncation in the repo -- 160, 180, 200, 240 and 300 --
    so it existed and no rep ever saw it.
    """
    monkeypatch.setattr(
        gw.requests, "get", lambda url, **kwargs: _invalid_field("Type")
    )

    with pytest.raises(requests.HTTPError) as excinfo:
        gateway._get("query", {"q": _OLD_SOQL})

    message = str(excinfo.value)
    assert len(_invalid_field("Type").json()[0]["message"]) > 350, "fixture too short"
    for cut in (160, 180, 200, 240, 300):
        assert "Retrying will not help" in message[:cut], f"lost at [:{cut}]"


def test_the_echoed_query_is_stripped_so_a_colleagues_email_does_not_reach_slack(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Salesforce echoes the failing SOQL, which on the create path inlines a rep."""
    leaky = (
        "SELECT Id,Name,Username FROM User "
        "WHERE Email='kerry@monarchconnected.com' AND IsActive=true"
    )
    monkeypatch.setattr(
        gw.requests, "get", lambda url, **kwargs: _invalid_field("IsActive", leaky)
    )

    with pytest.raises(requests.HTTPError) as excinfo:
        gateway._get("query", {"q": leaky})

    assert "kerry@monarchconnected.com" not in str(excinfo.value)
    assert "No such column 'IsActive'" in str(excinfo.value)


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

    assert "Retrying will not help" not in str(excinfo.value)
    assert "SERVER_UNAVAILABLE" in str(excinfo.value)


def test_grants_own_bad_query_is_never_blamed_on_the_customers_permissions() -> None:
    """A malformed query is permanent AND ours; a rep must not be sent to an admin."""
    response = _Response(
        400, [{"message": "unexpected token: 'FROMM'", "errorCode": "MALFORMED_QUERY"}]
    )

    note = salesforce_rest.permanence_note(response)
    guidance = salesforce_rest.permanent_failure_guidance(
        requests.HTTPError("x", response=response)
    )

    assert "retrying will not help" in note.lower()
    assert "defect in Grant" in note
    assert "field-level security" not in note
    assert "bug in Grant" in guidance
    assert "Salesforce admin" not in guidance.replace(
        "do NOT send the rep to a Salesforce admin", ""
    )


def test_a_write_side_permission_failure_is_classified_permanent() -> None:
    """The likeliest failure of the rep's NEXT step: adding members to a Campaign."""
    response = _Response(
        400,
        [
            {
                "message": "insufficient access rights on cross-reference id",
                "errorCode": "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY",
            }
        ],
    )

    assert salesforce_rest.is_permanent_error(response)
    assert salesforce_rest.is_access_error(response)


def test_a_composite_per_record_failure_reports_under_status_code() -> None:
    """Batched Lead and CampaignMember creates use `statusCode`, not `errorCode`."""
    response = _Response(
        400,
        [
            {
                "message": "No such column 'Type'",
                "statusCode": "INVALID_FIELD_FOR_INSERT_UPDATE",
            }
        ],
    )

    assert salesforce_rest.error_codes(response) == {"INVALID_FIELD_FOR_INSERT_UPDATE"}
    assert salesforce_rest.is_access_error(response)


def test_a_rejected_create_carries_the_reason_not_a_bare_status(
    gateway: gw.SalesforceCampaignGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create path returns a result instead of raising, and was never improved.

    This is the Campaign, Lead, CampaignMemberStatus and ContentNote create -- the
    rep's next click after the search this fix unblocked.
    """
    monkeypatch.setattr(
        gw.requests,
        "post",
        lambda url, **kwargs: _Response(
            400,
            [
                {
                    "message": "Unable to create/update fields: Type.",
                    "errorCode": "INVALID_FIELD_FOR_INSERT_UPDATE",
                }
            ],
        ),
    )

    result = gateway.create_campaign({"Name": "OREGON grants", "Type": "Other"})

    assert result.success is False
    assert "INVALID_FIELD_FOR_INSERT_UPDATE" in result.error
    assert "Type" in result.error
    assert "Retrying will not help" in result.error


def test_a_permission_failure_forbids_the_retry_advice_that_looped_a_rep() -> None:
    """The judgement travels with the error, because the response is gone by then."""
    error = requests.HTTPError("x", response=_invalid_field("Type"))

    guidance = salesforce_rest.permanent_failure_guidance(error)

    assert "Do NOT suggest waiting, trying again" in guidance
    assert "Salesforce admin" in guidance


def test_a_transient_failure_gets_no_permanence_guidance() -> None:
    """Control: a real outage must still be describable as one."""
    response = _Response(503, [{"message": "down", "errorCode": "SERVER_UNAVAILABLE"}])

    assert (
        salesforce_rest.permanent_failure_guidance(
            requests.HTTPError("x", response=response)
        )
        == ""
    )
    assert salesforce_rest.permanent_failure_guidance(ValueError("other")) == ""


def test_an_unreadable_picklist_forbids_asking_the_rep_to_guess() -> None:
    """The exact loop: Grant asked a rep which Types her org had enabled."""
    guidance = salesforce_rest.permanent_failure_guidance(
        salesforce_rest.PicklistNotReadable("Type withheld")
    )

    assert "Do NOT ask the rep which values" in guidance
    assert "admin" in guidance


def test_the_campaign_search_tool_hands_the_model_the_permanence_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end at the tool boundary, at production body length."""
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
    assert "Do NOT suggest waiting" in result
    assert "Do NOT suggest waiting" not in for_human(result), "guidance is model-only"
