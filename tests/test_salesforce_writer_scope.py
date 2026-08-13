"""Salesforce WRITER-scope tests: which org Grant may write to, and what it may create.

Split from test_salesforce_campaigns.py at the 1,000-line cap (CLAUDE.md rule 4).
The boundary is a real one: everything here is about AUTHORISATION — the token cache
being scoped to a configured org, the create allowlist, and the identity checks that
must fail closed — while the file it left is about building and approving campaign
previews. They fail for different reasons and are read at different times.
"""

from __future__ import annotations


import pytest

from grant_watch.enrich import salesforce_campaign_gateway as gateway_mod


def test_writer_token_cache_is_scoped_to_configured_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing writer org/Connected App cannot reuse another org's token."""
    cache = gateway_mod._TOKEN_CACHE
    original = (
        cache.token,
        cache.instance_url,
        cache.expires_at,
        cache.credential_scope,
    )
    calls: list[str] = []

    class Response:
        """OAuth response tied to the requested writer domain."""

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

    monkeypatch.setattr(gateway_mod.requests, "post", post)
    monkeypatch.setenv("SALESFORCE_WRITE_CLIENT_SECRET", "secret")
    try:
        gateway = gateway_mod.SalesforceCampaignGateway()
        for suffix in ("one", "two"):
            monkeypatch.setenv(
                "SALESFORCE_WRITE_MY_DOMAIN_URL", f"https://{suffix}.test"
            )
            monkeypatch.setenv("SALESFORCE_WRITE_CLIENT_ID", f"client-{suffix}")
            gateway._auth()
        assert calls == ["https://one.test", "https://two.test"]
    finally:
        (cache.token, cache.instance_url, cache.expires_at, cache.credential_scope) = (
            original
        )


def test_gateway_has_no_forbidden_object_create_path() -> None:
    """Even internal calls cannot create/update an Account or Opportunity."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    with pytest.raises(ValueError, match="forbidden"):
        gateway._create_one("Account", {"Name": "Do not create"})
    with pytest.raises(ValueError, match="forbidden"):
        gateway._create_many("Opportunity", [{"Name": "Do not create"}])


def test_writer_scope_verifies_exact_sandbox_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Salesforce's own org identity must match every configured write boundary."""

    class Response:
        """Return one authoritative sandbox Organization row."""

        def raise_for_status(self) -> None:
            """Model a successful identity query."""

        def json(self) -> dict[str, object]:
            """Return the allowlisted sandbox identity."""
            return {
                "records": [
                    {
                        "Id": "00D000000000001AAA",
                        "Name": "Monarch Sandbox",
                        "IsSandbox": True,
                        "InstanceName": "TEST1",
                    }
                ]
            }

    host = "https://example--monarchdev.sandbox.my.salesforce.com"
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", host))
    monkeypatch.setattr(gateway_mod.requests, "get", lambda *_a, **_k: Response())
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", host)
    monkeypatch.setenv("SALESFORCE_WRITE_ORG_ID", "00D000000000001AAA")
    monkeypatch.setenv("SALESFORCE_WRITE_EXPECT_SANDBOX", "1")
    identity = gateway.verify_write_scope()
    assert identity.organization_id == "00D000000000001AAA"
    assert identity.is_sandbox is True


@pytest.mark.parametrize(
    ("org_id", "expect_sandbox", "actual_sandbox", "message"),
    (
        ("", "1", True, "ORG_ID is not configured"),
        ("00D000000000002AAA", "1", True, "Organization ID"),
        ("00D000000000001AAA", "1", False, "sandbox status"),
        ("00D000000000001AAA", "", True, "must be explicitly"),
    ),
)
def test_writer_scope_fails_closed_on_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    org_id: str,
    expect_sandbox: str,
    actual_sandbox: bool,
    message: str,
) -> None:
    """Missing or conflicting org identity cannot reach a create request."""

    class Response:
        """Return a configurable Organization identity."""

        def raise_for_status(self) -> None:
            """Model a successful HTTP response."""

        def json(self) -> dict[str, object]:
            """Return the live identity used for comparison."""
            return {
                "records": [
                    {
                        "Id": "00D000000000001AAA",
                        "Name": "Monarch",
                        "IsSandbox": actual_sandbox,
                        "InstanceName": "TEST1",
                    }
                ]
            }

    host = "https://example--monarchdev.sandbox.my.salesforce.com"
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(gateway, "_auth", lambda: ("token", host))
    monkeypatch.setattr(gateway_mod.requests, "get", lambda *_a, **_k: Response())
    monkeypatch.setenv("SALESFORCE_WRITE_MY_DOMAIN_URL", host)
    monkeypatch.setenv("SALESFORCE_WRITE_ORG_ID", org_id)
    monkeypatch.setenv("SALESFORCE_WRITE_EXPECT_SANDBOX", expect_sandbox)
    with pytest.raises(PermissionError, match=message):
        gateway.verify_write_scope()


def test_writer_scope_rejects_oauth_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OAuth cannot redirect writer credentials to another Salesforce host."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(
        gateway,
        "_auth",
        lambda: ("token", "https://production.my.salesforce.com"),
    )
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL",
        "https://example--monarchdev.sandbox.my.salesforce.com",
    )
    monkeypatch.setenv("SALESFORCE_WRITE_ORG_ID", "00D000000000001AAA")
    monkeypatch.setenv("SALESFORCE_WRITE_EXPECT_SANDBOX", "1")
    with pytest.raises(PermissionError, match="does not match"):
        gateway.verify_write_scope()


class _FillTransport:
    """Records what a fill would actually send to Salesforce."""

    def __init__(self, existing: dict[str, object]) -> None:
        """Start from what the Lead already holds."""
        self.existing = {
            **existing,
            "LastModifiedDate": "2026-08-13T12:34:56.000+0000",
        }
        self.patched: dict[str, object] | None = None
        self.patch_headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> object:
        """Return the current field values."""
        del url, kwargs
        return _Response(200, self.existing)

    def patch(self, url: str, **kwargs: object) -> object:
        """Capture the write instead of performing it."""
        del url
        self.patched = dict(kwargs.get("json") or {})
        self.patch_headers = dict(kwargs.get("headers") or {})
        return _Response(204, {})


class _Response:
    """Minimal requests-shaped response."""

    def __init__(self, status: int, body: dict[str, object]) -> None:
        """Hold a status and a JSON body."""
        self.status_code = status
        self._body = body
        self.text = ""

    def json(self) -> dict[str, object]:
        """Return the body."""
        return self._body


def _gateway_with(
    monkeypatch: pytest.MonkeyPatch, transport: _FillTransport
) -> gateway_mod.SalesforceCampaignGateway:
    """A gateway whose auth and scope checks are stubbed, transport captured."""
    gateway = gateway_mod.SalesforceCampaignGateway()
    monkeypatch.setattr(gateway, "_auth", lambda: ("tok", "https://writer.test"))
    monkeypatch.setattr(gateway, "verify_write_scope", lambda: None)
    monkeypatch.setattr(gateway_mod.requests, "get", transport.get)
    monkeypatch.setattr(gateway_mod.requests, "patch", transport.patch)
    return gateway


def test_filling_a_lead_never_overwrites_a_field_that_has_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE safety property, and it is structural rather than a policy check.

    Grant is create-only on purpose — that is why "delete that campaign" is
    impossible rather than merely refused. This is the one narrow exception, and it
    is shaped so it cannot destroy anything: the record is READ first and only
    genuinely empty fields are sent, whatever the caller passes.
    """
    transport = _FillTransport(
        {"Street": "204 W Forest Home Ave", "Title": "", "MobilePhone": None}
    )
    gateway = _gateway_with(monkeypatch, transport)

    result = gateway.fill_lead_blanks(
        "00Q2M000019GMBvUAO",
        {
            "Street": "SOMEWHERE ELSE",  # already set — must NOT be sent
            "Title": "Director of Technology",  # empty — may be filled
            "MobilePhone": "555-0100",  # null — may be filled
        },
    )
    assert result.success
    assert transport.patched == {
        "Title": "Director of Technology",
        "MobilePhone": "555-0100",
    }, f"an existing value would have been overwritten: {transport.patched}"
    assert transport.patch_headers["If-Unmodified-Since"] == (
        "Thu, 13 Aug 2026 12:34:56 GMT"
    )


def test_filling_a_lead_cannot_touch_identity_or_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Company, OwnerId and Status change what a record IS and who owns it.

    They are absent from the allowlist deliberately: filling them would silently
    reassign a colleague's record.
    """
    transport = _FillTransport({"Company": "", "OwnerId": "", "Status": "", "City": ""})
    gateway = _gateway_with(monkeypatch, transport)

    gateway.fill_lead_blanks(
        "00Q2M000019GMBvUAO",
        {
            "Company": "Hijacked Inc",
            "OwnerId": "005000000000999",
            "Status": "Closed",
            "City": "Bellaire",
        },
    )
    assert transport.patched == {"City": "Bellaire"}, (
        f"a forbidden field reached Salesforce: {transport.patched}"
    )


def test_filling_a_lead_never_clears_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty proposed value must be dropped, never written as a blank."""
    transport = _FillTransport({"Title": "", "City": ""})
    gateway = _gateway_with(monkeypatch, transport)

    result = gateway.fill_lead_blanks(
        "00Q2M000019GMBvUAO", {"Title": "", "City": "   "}
    )
    assert transport.patched is None, "a blank was written into Salesforce"
    assert result.success and "nothing to fill" in (result.error or "")
