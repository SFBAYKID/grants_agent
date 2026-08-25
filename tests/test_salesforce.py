"""Offline Salesforce read tests for truth states and Account-bound Opportunities."""

from __future__ import annotations

import pytest
import requests

from grant_watch.enrich import salesforce


def test_one_word_overlap_is_never_high_confidence() -> None:
    """Orange USD must not become Orange County Water Authority."""
    confidence = salesforce._confidence(
        "Orange Unified School District",
        "Orange County Water Authority",
        "CA",
        "CA",
        "",
        "",
        "",
        "",
    )
    assert confidence == "possible"


def test_exact_name_state_mismatch_is_visible_but_never_confirmed() -> None:
    """A conflicting state is shown for human review, never marked high confidence."""
    assert (
        salesforce._confidence(
            "Castle Rock School District",
            "Castle Rock School District",
            "WA",
            "CO",
            "",
            "",
            "",
            "",
        )
        == "possible"
    )


def test_trailing_district_number_can_match_numberless_crm_name() -> None:
    """Source district identifiers may be absent from a Salesforce company name."""
    assert (
        salesforce._confidence(
            "Castle Rock School District 401",
            "Castle Rock School District",
            "WA",
            "WA",
            "",
            "",
            "",
            "",
        )
        == "high"
    )
    assert salesforce.search_terms("Castle Rock School District 401") == (
        "Castle Rock School District 401",
        "Castle Rock 401",
        "Castle Rock",
    )


def test_shared_place_name_with_extra_identity_words_is_only_possible() -> None:
    """Castle Rock Charter Foundation must not become a confirmed district match."""
    assert (
        salesforce._confidence(
            "Castle Rock School District 401",
            "Castle Rock Charter Foundation",
            "WA",
            "WA",
            "",
            "",
            "",
            "",
        )
        == "possible"
    )


def test_account_outage_is_unavailable_not_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Account query cannot support a net-new claim."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))

    def broken(*_args: object) -> list[dict[str, object]]:
        """Provide test-local behavior for broken."""
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


def test_open_opportunity_is_queried_through_confirmed_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opportunity context must carry the exact matched AccountId."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))
    monkeypatch.setattr(
        salesforce,
        "_query_accounts",
        lambda *_args: [
            {
                "Id": "001MATCH",
                "Name": "Castle Rock School District",
                "BillingState": "WA",
                "Website": "https://crschools.org",
                "Phone": "",
                "Owner": {"Name": "Anthony"},
            }
        ],
    )
    monkeypatch.setattr(salesforce, "_query_people", lambda *_args: [])
    seen: list[str] = []

    def opportunities(account_id: str, *_args: object) -> list[dict[str, object]]:
        """Provide test-local behavior for opportunities."""
        seen.append(account_id)
        return [
            {
                "Id": "006OPP",
                "Name": "Security Upgrade",
                "StageName": "Prospecting",
                "IsClosed": False,
                "AccountId": account_id,
                "Owner": {"Name": "Anthony"},
            }
        ]

    monkeypatch.setattr(salesforce, "_query_opportunities", opportunities)
    result = salesforce.lookup(
        "Castle Rock School District",
        state="WA",
        domain="crschools.org",
    )
    assert result.status is salesforce.SFResultStatus.FOUND
    assert seen == ["001MATCH"]
    opportunity = [match for match in result.matches if match.sobject == "Opportunity"][
        0
    ]
    assert opportunity.account_id == "001MATCH"


def test_secondary_outage_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Account data can be returned while disclosing incomplete secondary queries."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))
    monkeypatch.setattr(salesforce, "_query_accounts", lambda *_args: [])

    def broken(*_args: object) -> list[dict[str, object]]:
        """Provide test-local behavior for broken."""
        raise requests.Timeout("down")

    monkeypatch.setattr(salesforce, "_query_people", broken)
    result = salesforce.lookup("Test District", state="CA")
    assert result.status is salesforce.SFResultStatus.PARTIAL


def test_confirmed_account_with_multiple_contacts_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expected contacts under one Account do not erase verified org identity."""
    monkeypatch.setattr(salesforce, "_auth", lambda: ("token", "https://sf.test"))
    monkeypatch.setattr(
        salesforce,
        "_query_accounts",
        lambda *_args: [
            {
                "Id": "001MATCH",
                "Name": "Castle Rock School District",
                "BillingState": "WA",
                "Website": "https://crschools.org",
                "Phone": "",
                "Owner": {"Name": "Anthony"},
            }
        ],
    )
    monkeypatch.setattr(
        salesforce,
        "_query_people",
        lambda *_args: [
            {
                "Id": f"003{index}",
                "Name": f"Person {index}",
                "MailingState": "WA",
                "Phone": "",
                "Owner": {"Name": "Anthony"},
                "Account": {"Id": "001MATCH", "Name": "Castle Rock School District"},
                "attributes": {"type": "Contact"},
            }
            for index in range(2)
        ],
    )
    monkeypatch.setattr(salesforce, "_query_opportunities", lambda *_args: [])
    result = salesforce.lookup(
        "Castle Rock School District", state="WA", domain="crschools.org"
    )
    assert result.status is salesforce.SFResultStatus.FOUND


def test_reader_token_cache_is_scoped_to_configured_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing reader org/Connected App cannot reuse another org's token."""
    cache = salesforce._TOKEN_CACHE
    original = (
        cache.access_token,
        cache.instance_url,
        cache.expires_at,
        cache.credential_scope,
    )
    calls: list[str] = []

    class Response:
        """OAuth response tied to the requested domain."""

        def __init__(self, domain: str) -> None:
            """Initialize the test double."""
            self.domain = domain

        def raise_for_status(self) -> None:
            """Model a successful OAuth response."""

        def json(self) -> dict[str, str]:
            """Return a distinct token/instance per domain."""
            return {"access_token": f"token-{self.domain}", "instance_url": self.domain}

    def post(url: str, **_kwargs: object) -> Response:
        """Provide test-local behavior for post."""
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
        (
            cache.access_token,
            cache.instance_url,
            cache.expires_at,
            cache.credential_scope,
        ) = original


def test_a_hash_numbered_district_matches_the_same_district_without_the_hash() -> None:
    """ "#428" and "428" name ONE district, and the matcher must agree they do.

    `"#428".isdigit()` is False while `"428".isdigit()` is True, so the entity kept
    `#428` as a required identity token and the Salesforce record dropped `428`. The
    sets could never be equal, and `_confidence` only reaches "high" on equality --
    so the correctly-named record could only ever be "possible", which
    `_resolve_existing_record` refuses to act on. School district names carry "#NNN"
    constantly; both leads that failed in production (DeKalb #428, Baboquivari #40)
    are of exactly this shape.
    """
    hashed = "Dekalb Community Unit School District #428"
    plain = "Dekalb Community Unit School District 428"
    assert salesforce._tokens(hashed) == salesforce._tokens(plain)
    assert salesforce._confidence(hashed, plain, "IL", "IL", "", "", "", "") == "high"
    # Control: a DIFFERENT district must not be promoted by the same change.
    assert (
        salesforce._confidence(
            hashed,
            "Sycamore Community Unit School District 427",
            "IL",
            "IL",
            "",
            "",
            "",
            "",
        )
        != "high"
    )


def test_the_tolerant_search_variant_is_generated_for_a_hash_numbered_district() -> (
    None
):
    """The most tolerant SOSL fallback was silently never built for "#NNN" names.

    `search_terms` dropped bare digits to build its widest variant, but `#428` is not
    a bare digit -- so that variant came out identical to the previous one, was
    de-duplicated away, and the broad search that would find the organization by name
    alone was never run. A lookup can then return nothing while the record exists.
    """
    terms = salesforce.search_terms("Dekalb Community Unit School District #428")
    assert "Dekalb Community Unit" in terms
    assert len(terms) == 3, terms
    # Control: a name with no record number still yields its own bounded variants
    # rather than being padded out to three.
    plain = salesforce.search_terms("Baboquivari Unified School District")
    assert all(term for term in plain)
    assert len(plain) == len(set(plain))


def test_the_same_state_written_two_ways_is_not_a_conflict() -> None:
    """ "IL" and "Illinois" are one state, and a hand-maintained CRM holds both.

    Measured in production 2026-08-25: six Leads for ONE district, carrying `IL` on
    the 2019 record and `Illinois` on the 2023/2024 ones. `_confidence` compared the
    raw strings, called that a state conflict, and a conflict leaves exact token
    equality as the only route to "high" -- which the `#428` defect independently
    guaranteed would fail. Both had to be fixed; either alone yields only "possible",
    which `_resolve_existing_record` refuses to act on.
    """
    entity = "Dekalb Community Unit School District #428"
    assert (
        salesforce._confidence(
            entity,
            "Dekalb Community Unit School District 428",
            "IL",
            "Illinois",
            "",
            "",
            "",
            "",
        )
        == "high"
    )


def test_a_genuine_state_conflict_is_still_never_promoted() -> None:
    """The control. Normalizing must not erase real geography.

    An identically-named organization in another state stays un-promoted, and so
    does a state string neither side can parse -- an unrecognized value must fail
    safe rather than be treated as "no state on file" and silently waved through.
    """
    entity = "Dekalb Community Unit School District #428"
    same_name = "Dekalb Community Unit School District 428"
    for candidate_state in ("Texas", "TX", "Ontario"):
        assert (
            salesforce._confidence(
                entity, same_name, "IL", candidate_state, "", "", "", ""
            )
            != "high"
        ), candidate_state


def test_four_duplicate_crm_records_refuse_rather_than_pick_one() -> None:
    """The real DeKalb shape: fixing the matcher must not license a guess.

    Production holds FOUR identical Leads for this district. Surfacing them is the
    fix working; choosing among them is not Grant's call, and creating a fifth is
    the outcome the rep was already complaining about.
    """
    entity = "Dekalb Community Unit School District #428"
    production_rows = [
        ("DeKalb CUSD 428", "IL"),
        ("Dekalb Community Unit School District 428", "Illinois"),
        ("Dekalb Community Unit School District 428", "Illinois"),
        ("Dekalb Community Unit School District 428", "Illinois"),
        ("Dekalb Community Unit School District 428", "Illinois"),
        ("DeKalb School District 428", "Illinois"),
    ]
    scored = [
        salesforce._confidence(entity, company, "IL", state, "", "", "", "")
        for company, state in production_rows
    ]
    assert scored.count("high") == 4
    # Every remaining row is still surfaced to a human rather than discarded.
    assert None not in scored


def test_a_one_word_district_still_matches_on_its_record_number() -> None:
    """The regression that stripping "#" introduced, caught in review before shipping.

    "Baboquivari Unified School District #40" has ONE distinctive word once the
    generic organization words are removed. The deployed code cleared the two-token
    threshold only by accident -- `'#40'.isdigit()` is False, so `#40` counted as a
    name word. Normalizing the "#" away therefore took the name UNDER the threshold
    and no candidate could ever be confident again: a silent refusal, which reads
    exactly like correct caution. Measured 2026-08-25, that would have hit 14 of the
    34 production leads carrying a "#".

    The CRM record also carries a stray internal code, which is why numbers are
    matched by INTERSECTION and not by equality.
    """
    entity = "Baboquivari Unified School District #40"
    for candidate in (
        "BABOQUIVARI UNIFIED SCHOOL DISTRICT #40 (4412)",
        "Baboquivari Unified School District 40",
    ):
        assert (
            salesforce._confidence(entity, candidate, "AZ", "AZ", "", "", "", "")
            == "high"
        ), candidate
    # Control: the number is the only thing distinguishing these, so a DIFFERENT
    # number must not be promoted however well the words agree.
    assert (
        salesforce._confidence(
            entity,
            "Baboquivari Unified School District #55",
            "AZ",
            "AZ",
            "",
            "",
            "",
            "",
        )
        != "high"
    )


def test_the_record_number_can_carry_an_identity_the_name_cannot() -> None:
    """ "#492" IS the identity -- the words alone name thousands of districts.

    The deployed code scores this only "possible", because the entity keeps `#492`
    while the CRM record drops `492`, so the sets differ. Treating the number as its
    own dimension fixes a case that was already wrong before today.
    """
    entity = "INDEPENDENT SCHOOL DISTRICT #492"
    assert (
        salesforce._confidence(
            entity, "Independent School District 492", "MN", "MN", "", "", "", ""
        )
        == "high"
    )
    assert (
        salesforce._confidence(
            entity, "INDEPENDENT SCHOOL DISTRICT #625", "MN", "MN", "", "", "", ""
        )
        != "high"
    )
