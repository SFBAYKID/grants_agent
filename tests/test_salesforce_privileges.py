"""The account, not the code, must be the thing that cannot delete.

Every other guard in the gateway is code asking code to behave. These pin the one
control that survives a bug we write later: Salesforce itself refusing the account.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from grant_watch.enrich import salesforce_privileges as priv


def _getter(
    *, deletable: bool, perms: dict[str, Any] | None = None, fail_profile: bool = False
) -> priv.JsonGetter:
    """A Salesforce double that answers describe and Profile like the real API."""

    def get_json(path: str, params: dict[str, str] | None) -> dict[str, Any]:
        """Answer describe and Profile the way the live REST API does."""
        if path.endswith("/describe"):
            return {"createable": True, "updateable": True, "deletable": deletable}
        if path == "query":
            if fail_profile:
                raise RuntimeError("INSUFFICIENT_ACCESS: cannot read Profile")
            return {"records": [perms or {}]}
        raise AssertionError(f"unexpected path {path}")

    return get_json


def test_an_account_that_can_delete_is_refused() -> None:
    """THE property. Measured 2026-08-22, the live account failed exactly this."""
    report = priv.audit_privileges(
        _getter(deletable=True),
        "https://x.sandbox.my.salesforce.com",
        "admin@x",
        "System Administrator",
    )
    assert not report.is_safe
    assert len(report.deletable_objects) == len(priv.GRANT_OBJECTS)
    with pytest.raises(PermissionError, match="Refusing to write"):
        priv.assert_write_safe(report)


def test_a_least_privilege_account_is_allowed() -> None:
    """The control. Without this the guard could 'pass' by refusing everything."""
    report = priv.audit_privileges(
        _getter(deletable=False, perms={}),
        "https://x",
        "agent.quotes.sales@x",
        "Minimum Access",
    )
    assert report.deletable_objects == ()
    assert report.is_safe
    priv.assert_write_safe(report)  # must not raise


@pytest.mark.parametrize(
    "flag", ["PermissionsModifyAllData", "PermissionsBulkApiHardDelete"]
)
def test_a_system_permission_alone_is_enough_to_refuse(flag: str) -> None:
    """Delete-free objects are not safety if the account can override sharing.

    Modify All Data reaches every record regardless of per-object settings, so an
    audit that only looked at `deletable` would report a false all-clear.
    """
    report = priv.audit_privileges(
        _getter(deletable=False, perms={flag: True}),
        "https://x",
        "admin@x",
        "System Administrator",
    )
    assert report.deletable_objects == ()
    assert not report.is_safe
    with pytest.raises(PermissionError, match=flag):
        priv.assert_write_safe(report)


def test_an_unreadable_profile_never_fakes_a_violation() -> None:
    """A least-privilege account often cannot read Profile, and that is not a fault.

    Unknown must behave as "not proven set" rather than "proven dangerous", or the
    correctly-configured account would be the one that gets refused.
    """
    report = priv.audit_privileges(
        _getter(deletable=False, fail_profile=True),
        "https://x",
        "agent@x",
        "Minimum Access",
    )
    assert report.system_permissions == {}
    assert report.is_safe


def test_the_audit_is_cached_but_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-describing 11 objects before every write would multiply the call count."""
    priv.reset_cache()
    calls = {"n": 0}

    def counting(path: str, params: dict[str, str] | None) -> dict[str, Any]:
        """Count every call so a cache hit is distinguishable from a miss."""
        calls["n"] += 1
        return {
            "createable": True,
            "updateable": True,
            "deletable": False,
            "records": [{}],
        }

    clock = {"t": 1000.0}
    for _ in range(3):
        priv.cached_audit(counting, "https://x", now=lambda: clock["t"])
    first = calls["n"]
    assert first > 0, "the first audit must actually call Salesforce"

    clock["t"] += priv.AUDIT_TTL_SECONDS + 1
    priv.cached_audit(counting, "https://x", now=lambda: clock["t"])
    assert calls["n"] > first, "an expired audit must be re-measured, not trusted"
    priv.reset_cache()


def test_no_delete_verb_can_enter_the_codebase() -> None:
    """Structural: the create-only guarantee, enforced instead of merely observed.

    Grant is create-only on purpose -- it is why "delete that campaign" is impossible
    rather than refused. Nothing enforced that until now: a single `requests.delete`
    would have shipped with a green suite. `_HttpMethod` and an explicit method string
    are included because Salesforce accepts both as a DELETE without the verb.
    """
    forbidden = (
        "requests.delete(",
        'method="DELETE"',
        "method='DELETE'",
        "_HttpMethod",
    )
    offenders: list[str] = []
    for path in sorted(Path("grant_watch").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in source:
                offenders.append(f"{path}: {needle}")
    assert not offenders, (
        "Grant must never be able to delete a Salesforce record. Found: "
        + "; ".join(offenders)
    )


def test_the_preflight_falls_back_to_the_reader_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRODUCTION sets none of the SALESFORCE_WRITE_* keys, and that is correct.

    One connected app serves both roles there; the gateway has always fallen back to
    the reader. Subscripting the writer keys directly made the preflight KeyError on
    the droplet while passing on a laptop that happens to set them -- and the droplet
    is exactly where you run it after changing the integration user.
    """
    for key in (
        "SALESFORCE_WRITE_MY_DOMAIN_URL",
        "SALESFORCE_WRITE_CLIENT_ID",
        "SALESFORCE_WRITE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SALESFORCE_MY_DOMAIN_URL", "https://reader.my.salesforce.com/")
    monkeypatch.setenv("SALESFORCE_CLIENT_ID", "reader-id")
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "reader-secret")

    assert priv.writer_credentials() == (
        "https://reader.my.salesforce.com",
        "reader-id",
        "reader-secret",
    )


def test_an_explicit_writer_credential_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: a separately-credentialed writer must NOT be overridden.

    Without this the fallback could 'pass' by ignoring the writer entirely, which on
    a machine with a sandbox writer would silently audit the wrong org.
    """
    monkeypatch.setenv("SALESFORCE_MY_DOMAIN_URL", "https://reader.my.salesforce.com")
    monkeypatch.setenv("SALESFORCE_CLIENT_ID", "reader-id")
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "reader-secret")
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.sandbox.my.salesforce.com"
    )
    monkeypatch.setenv("SALESFORCE_WRITE_CLIENT_ID", "writer-id")
    monkeypatch.setenv("SALESFORCE_WRITE_CLIENT_SECRET", "writer-secret")

    assert priv.writer_credentials() == (
        "https://writer.sandbox.my.salesforce.com",
        "writer-id",
        "writer-secret",
    )
