"""ZoomInfo transport tests — auth, free search parsing, and paid-call guards.

No test here touches the network: every request is stubbed. The point of these is the
honesty and cost invariants, not vendor round-trips — an outage must never read as
"no contacts", and nothing may spend a credit without going through the guards.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from grant_watch.enrich import zoominfo


@pytest.fixture(autouse=True)
def _reset_token_cache() -> None:
    """Clear the process-local token cache so tests cannot leak state into each other."""
    zoominfo._TOKEN_CACHE.access_token = ""
    zoominfo._TOKEN_CACHE.expires_at = 0.0
    zoominfo._TOKEN_CACHE.credential_scope = ""


class _Response:
    """Minimal stand-in for requests.Response covering the client's usage."""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def _configure(monkeypatch: pytest.MonkeyPatch, client_id: str = "cid") -> None:
    monkeypatch.setenv("ZOOMINFO_CLIENT_ID", client_id)
    monkeypatch.setenv("ZOOMINFO_CLIENT_SECRET", "shhh")


def test_missing_credentials_report_names_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured install explains WHICH var is absent and never echoes a secret."""
    monkeypatch.delenv("ZOOMINFO_CLIENT_ID", raising=False)
    monkeypatch.setenv("ZOOMINFO_CLIENT_SECRET", "super-secret-value")
    assert zoominfo.configured() is False
    with pytest.raises(zoominfo.ZoomInfoConfigurationError) as excinfo:
        zoominfo._auth()
    assert "ZOOMINFO_CLIENT_ID" in str(excinfo.value)
    assert "super-secret-value" not in str(excinfo.value)


def test_token_request_sends_the_cloudflare_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UA header is load-bearing: Cloudflare 1010-blocks the default one.

    Without it every ZoomInfo call fails as an opaque 403 that reads like bad
    credentials, so this is pinned rather than left to convention.
    """
    _configure(monkeypatch)
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _Response:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers") or {}
        return _Response({"access_token": "tok", "expires_in": 86400})

    monkeypatch.setattr(zoominfo.requests, "post", fake_post)
    assert zoominfo._auth() == "tok"
    assert seen["url"] == zoominfo.TOKEN_URL
    assert seen["headers"]["User-Agent"] == zoominfo.USER_AGENT
    assert seen["headers"]["Authorization"].startswith("Basic ")


def test_token_is_cached_and_reminted_when_the_credential_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached token is reused, but a different client id must never reuse it."""
    _configure(monkeypatch)
    calls: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        calls.append(url)
        return _Response({"access_token": f"tok{len(calls)}", "expires_in": 86400})

    monkeypatch.setattr(zoominfo.requests, "post", fake_post)
    assert zoominfo._auth() == "tok1"
    assert zoominfo._auth() == "tok1"  # served from cache, no second mint
    assert len(calls) == 1
    _configure(monkeypatch, client_id="a-different-app")
    assert zoominfo._auth() == "tok2"
    assert len(calls) == 2


def test_transport_failure_raises_unavailable_not_an_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage must be distinguishable from "this org has no contacts"."""
    _configure(monkeypatch)

    def fake_post(url: str, **kwargs: Any) -> _Response:
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(zoominfo.requests, "post", fake_post)
    with pytest.raises(zoominfo.ZoomInfoUnavailable):
        zoominfo.search_contacts("Some School District")


def test_token_response_without_a_token_is_an_outage_not_a_silent_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 carrying no access_token must not become an empty bearer header."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        zoominfo.requests, "post", lambda url, **kwargs: _Response({"expires_in": 60})
    )
    with pytest.raises(zoominfo.ZoomInfoUnavailable):
        zoominfo._auth()


def _search_payload() -> dict[str, Any]:
    """One realistic JSON:API search body, shaped like the live 2026-08-09 response."""
    return {
        "data": [
            {
                "id": "12345",
                "type": "Contact",
                "attributes": {
                    "firstName": "Dana",
                    "lastName": "Reyes",
                    "jobTitle": "Director of Technology",
                    "company": {"name": "Imperial Unified School District"},
                    "hasEmail": True,
                    "hasDirectPhone": False,
                    "hasMobilePhone": True,
                    "hasSupplementalEmail": False,
                    "directPhoneDoNotCall": False,
                    "mobilePhoneDoNotCall": True,
                    "contactAccuracyScore": "92",
                    "lastUpdatedDate": "2026-05-01",
                },
            }
        ],
        "meta": {"totalResults": 1},
    }


def test_search_parses_the_json_api_envelope_and_availability_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search results carry the has_* flags that make a free cost quote possible."""
    _configure(monkeypatch)
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        if url == zoominfo.TOKEN_URL:
            return _Response({"access_token": "tok", "expires_in": 86400})
        posts.append((url, kwargs.get("json") or {}))
        return _Response(_search_payload())

    monkeypatch.setattr(zoominfo.requests, "post", fake_post)
    matches = zoominfo.search_contacts(
        "Imperial Unified School District", state="ca", limit=5
    )
    assert len(matches) == 1
    match = matches[0]
    assert match.person_id == "12345"
    assert match.display_name == "Dana Reyes"
    assert match.job_title == "Director of Technology"
    assert match.company_name == "Imperial Unified School District"
    assert match.has_email is True
    assert match.has_direct_phone is False
    assert match.has_mobile_phone is True
    assert match.contact_accuracy_score == 92.0
    url, body = posts[0]
    assert "page%5Bsize%5D=5" in url
    assert body["data"]["type"] == "ContactSearch"
    # The flat body a naive client would send is a 400; the envelope is required.
    assert body["data"]["attributes"]["companyName"] == "Imperial Unified School District"
    assert body["data"]["attributes"]["state"] == "CA"


def test_search_returns_empty_for_a_genuine_no_coverage_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero results is an honest answer, not an exception (measured live: Hoxie AR)."""
    _configure(monkeypatch)

    def fake_post(url: str, **kwargs: Any) -> _Response:
        if url == zoominfo.TOKEN_URL:
            return _Response({"access_token": "tok", "expires_in": 86400})
        return _Response({"data": [], "meta": {"totalResults": 0}})

    monkeypatch.setattr(zoominfo.requests, "post", fake_post)
    assert zoominfo.search_contacts("Hoxie School District", state="AR") == []


def test_search_requires_a_company_name() -> None:
    """An unscoped contact search is refused before it can be billed or rate-limited."""
    with pytest.raises(ValueError, match="company_name"):
        zoominfo.search_contacts("   ")


def test_do_not_call_is_pessimistic_across_both_numbers() -> None:
    """One flagged line is enough — the caller cannot choose which number it dials."""
    match = zoominfo.ZoomInfoContactMatch(
        person_id="1",
        first_name="A",
        last_name="B",
        job_title="IT Director",
        company_name="X",
        has_email=True,
        has_direct_phone=True,
        has_mobile_phone=True,
        has_supplemental_email=False,
        direct_phone_do_not_call=False,
        mobile_phone_do_not_call=True,
    )
    assert match.do_not_call is True


def test_availability_flags_only_trust_real_booleans() -> None:
    """A vendor string like "false" must never be coerced into a truthy flag."""
    assert zoominfo._as_bool("false") is False
    assert zoominfo._as_bool("true") is False  # only literal True counts
    assert zoominfo._as_bool(None) is False
    assert zoominfo._as_bool(True) is True


def test_quote_counts_exactly_what_a_pull_would_cost() -> None:
    """The number a rep approves must equal the number of credits at risk."""

    def build(email: bool, mobile: bool, dnc: bool) -> zoominfo.ZoomInfoContactMatch:
        return zoominfo.ZoomInfoContactMatch(
            person_id="1",
            first_name="A",
            last_name="B",
            job_title="t",
            company_name="c",
            has_email=email,
            has_direct_phone=False,
            has_mobile_phone=mobile,
            has_supplemental_email=False,
            direct_phone_do_not_call=False,
            mobile_phone_do_not_call=dnc,
        )

    quote = zoominfo.quote(
        [build(True, True, False), build(False, True, True), build(True, False, False)]
    )
    assert quote.billable == 3
    assert quote.with_email == 2
    assert quote.with_phone == 2
    assert quote.do_not_call == 1


def test_enrich_refuses_more_than_the_vendor_batch_ceiling() -> None:
    """Over-sized batches are a 400 upstream; refuse locally before spending anything."""
    with pytest.raises(ValueError, match="at most 25"):
        zoominfo.enrich_contacts([str(n) for n in range(26)])


def test_enrich_of_nothing_makes_no_request_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty approval list must not authenticate, let alone bill."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no HTTP call may happen for an empty enrich list")

    monkeypatch.setattr(zoominfo.requests, "post", explode)
    assert zoominfo.enrich_contacts([]) == []


def test_billable_records_counts_only_full_matches() -> None:
    """NO_MATCH and OPT_OUT are free; charging a rep for them would overstate spend."""

    def build(status: str) -> zoominfo.ZoomInfoContactDetail:
        return zoominfo.ZoomInfoContactDetail(
            person_id="1",
            first_name="A",
            last_name="B",
            job_title="t",
            company_name="c",
            email="a@b.test",
            direct_phone="",
            mobile_phone="",
            direct_phone_do_not_call=False,
            mobile_phone_do_not_call=False,
            match_status=status,
        )

    details = [build("FULL_MATCH"), build("NO_MATCH"), build("OPT_OUT")]
    assert zoominfo.billable_records(details) == 1
    assert build("FULL_MATCH").matched is True
    assert build("NO_MATCH").matched is False
