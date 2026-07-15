"""Offline Salesforce read tests for truth states and Account-bound Opportunities."""

from __future__ import annotations

import pytest
import requests

from grant_watch.enrich import salesforce


def test_one_word_overlap_is_never_high_confidence() -> None:
    """Orange USD must not become Orange County Water Authority."""
    confidence = salesforce._confidence(
        "Orange Unified School District", "Orange County Water Authority",
        "CA", "CA", "", "", "", "",
    )
    assert confidence == "possible"


def test_exact_name_state_mismatch_is_visible_but_never_confirmed() -> None:
    """A conflicting state is shown for human review, never marked high confidence."""
    assert salesforce._confidence(
        "Castle Rock School District", "Castle Rock School District",
        "WA", "CO", "", "", "", "",
    ) == "possible"


def test_trailing_district_number_can_match_numberless_crm_name() -> None:
    """Source district identifiers may be absent from a Salesforce company name."""
    assert salesforce._confidence(
        "Castle Rock School District 401", "Castle Rock School District",
        "WA", "WA", "", "", "", "",
    ) == "high"
    assert salesforce.search_terms("Castle Rock School District 401") == (
        "Castle Rock School District 401", "Castle Rock 401", "Castle Rock")


def test_shared_place_name_with_extra_identity_words_is_only_possible() -> None:
    """Castle Rock Charter Foundation must not become a confirmed district match."""
    assert salesforce._confidence(
        "Castle Rock School District 401", "Castle Rock Charter Foundation",
        "WA", "WA", "", "", "", "",
    ) == "possible"


def test_account_outage_is_unavailable_not_no_match(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed Account query cannot support a net-new claim."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))

    def broken(*_args: object) -> list[dict[str, object]]:
        raise requests.Timeout("down")

    monkeypatch.setattr(salesforce, "_query_accounts", broken)
    result = salesforce.lookup("Test District", state="CA")
    assert result.status is salesforce.SFResultStatus.UNAVAILABLE
    assert result.matched is False
    assert result.error


def test_complete_empty_search_is_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only completed Account and people searches may return no_match."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))
    monkeypatch.setattr(salesforce, "_query_accounts", lambda *_args: [])
    monkeypatch.setattr(salesforce, "_query_people", lambda *_args: [])
    result = salesforce.lookup("Test District", state="CA")
    assert result.status is salesforce.SFResultStatus.NO_MATCH


def test_campaign_search_uses_reader_get_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Campaign discovery works without any writer configuration or mutation."""
    monkeypatch.delenv("SALESFORCE_WRITE_MY_DOMAIN_URL", raising=False)
    monkeypatch.setattr(salesforce, "_auth", lambda: ("reader", "https://sf.test"))
    calls: list[tuple[str, dict[str, str], str, str]] = []

    def read(path: str, params: dict[str, str], token: str,
             instance: str) -> dict[str, object]:
        calls.append((path, params, token, instance))
        return {"records": [{"Id": "701000000000001", "Name": "Just Testing"}]}

    monkeypatch.setattr(salesforce, "_readonly_get", read)
    matches = salesforce.search_campaigns("Just Testing")
    assert [match.name for match in matches] == ["Just Testing"]
    assert calls[0][0] == "query" and calls[0][2] == "reader"
    assert "Just Testing" in calls[0][1]["q"]


def test_campaign_link_rejects_another_salesforce_org(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A pasted Campaign link cannot cross the configured reader-org boundary."""
    monkeypatch.setenv("SALESFORCE_MY_DOMAIN_URL", "https://reader.salesforce.test")
    with pytest.raises(ValueError, match="configured reader org"):
        salesforce.get_campaign_from_link(
            "https://other.salesforce.test/lightning/r/Campaign/701000000000001/view")


def test_open_opportunity_is_queried_through_confirmed_account(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Opportunity context must carry the exact matched AccountId."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))
    monkeypatch.setattr(salesforce, "_query_accounts", lambda *_args: [{
        "Id": "001MATCH", "Name": "Castle Rock School District",
        "BillingState": "WA", "Website": "https://crschools.org",
        "Phone": "", "Owner": {"Name": "Anthony"},
    }])
    monkeypatch.setattr(salesforce, "_query_people", lambda *_args: [])
    seen: list[str] = []

    def opportunities(account_id: str, *_args: object) -> list[dict[str, object]]:
        seen.append(account_id)
        return [{
            "Id": "006OPP", "Name": "Security Upgrade", "StageName": "Prospecting",
            "IsClosed": False, "AccountId": account_id, "Owner": {"Name": "Anthony"},
        }]

    monkeypatch.setattr(salesforce, "_query_opportunities", opportunities)
    result = salesforce.lookup(
        "Castle Rock School District", state="WA", domain="crschools.org",
    )
    assert result.status is salesforce.SFResultStatus.FOUND
    assert seen == ["001MATCH"]
    opportunity = [match for match in result.matches if match.sobject == "Opportunity"][0]
    assert opportunity.account_id == "001MATCH"


def test_secondary_outage_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Account data can be returned while disclosing incomplete secondary queries."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))
    monkeypatch.setattr(salesforce, "_query_accounts", lambda *_args: [])

    def broken(*_args: object) -> list[dict[str, object]]:
        raise requests.Timeout("down")

    monkeypatch.setattr(salesforce, "_query_people", broken)
    result = salesforce.lookup("Test District", state="CA")
    assert result.status is salesforce.SFResultStatus.PARTIAL


def test_confirmed_account_with_multiple_contacts_is_found(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Expected contacts under one Account do not erase verified org identity."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))
    monkeypatch.setattr(salesforce, "_query_accounts", lambda *_args: [{
        "Id": "001MATCH", "Name": "Castle Rock School District",
        "BillingState": "WA", "Website": "https://crschools.org",
        "Phone": "", "Owner": {"Name": "Anthony"},
    }])
    monkeypatch.setattr(salesforce, "_query_people", lambda *_args: [{
        "Id": f"003{index}", "Name": f"Person {index}", "MailingState": "WA",
        "Phone": "", "Owner": {"Name": "Anthony"},
        "Account": {"Id": "001MATCH", "Name": "Castle Rock School District"},
        "attributes": {"type": "Contact"},
    } for index in range(2)])
    monkeypatch.setattr(salesforce, "_query_opportunities", lambda *_args: [])
    result = salesforce.lookup(
        "Castle Rock School District", state="WA", domain="crschools.org")
    assert result.status is salesforce.SFResultStatus.FOUND


def test_reader_token_cache_is_scoped_to_configured_org(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing reader org/Connected App cannot reuse another org's token."""
    cache = salesforce._TOKEN_CACHE
    original = (cache.access_token, cache.instance_url, cache.expires_at,
                cache.credential_scope)
    calls: list[str] = []

    class Response:
        """OAuth response tied to the requested domain."""

        def __init__(self, domain: str) -> None:
            self.domain = domain

        def raise_for_status(self) -> None:
            """Model a successful OAuth response."""

        def json(self) -> dict[str, str]:
            """Return a distinct token/instance per domain."""
            return {"access_token": f"token-{self.domain}",
                    "instance_url": self.domain}

    def post(url: str, **_kwargs: object) -> Response:
        domain = url.split("/services/", 1)[0]
        calls.append(domain)
        return Response(domain)

    monkeypatch.setattr(salesforce.requests, "post", post)
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "secret")
    try:
        for suffix in ("one", "two"):
            monkeypatch.setenv("SALESFORCE_MY_DOMAIN_URL", f"https://{suffix}.test")
            monkeypatch.setenv("SALESFORCE_CLIENT_ID", f"client-{suffix}")
            salesforce._auth()
        assert calls == ["https://one.test", "https://two.test"]
    finally:
        (cache.access_token, cache.instance_url, cache.expires_at,
         cache.credential_scope) = original
