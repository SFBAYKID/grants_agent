"""A copied environment is not host authority to call paid-data providers."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch import health
from grant_watch import paid_provider_authority as authority
from grant_watch import paid_provider_authority_init
from grant_watch.enrich import firecrawl_gateway, zoominfo
from tests.paid_provider_support import (
    configure_firecrawl_runtime,
    configure_zoominfo_runtime,
)


def test_authority_initializer_is_dry_by_default_and_never_replaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reviewed bootstrap command previews, creates privately, then refuses."""
    target = tmp_path / "private" / "authority.json"
    scopes = (("firecrawl", "account-firecrawl-001"),)
    assert paid_provider_authority_init.run(target, scopes, execute=False) == 0
    assert not target.exists()
    assert "no file changed" in capsys.readouterr().out

    assert paid_provider_authority_init.run(target, scopes, execute=True) == 0
    assert target.stat().st_mode & 0o077 == 0
    before = target.read_bytes()
    assert paid_provider_authority_init.run(target, scopes, execute=True) == 2
    assert target.read_bytes() == before


def test_copied_firecrawl_env_without_capability_fails_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential, ceiling, and ledger path alone cannot mint host authority."""
    monkeypatch.setenv(authority.MODE_ENV, "authority")
    monkeypatch.setenv(
        authority.AUTHORITY_FILE_ENV, str(tmp_path / "missing-authority.json")
    )
    monkeypatch.setenv("FIRECRAWL_API_KEY", "copied-secret")
    monkeypatch.setenv(firecrawl_gateway.MONTHLY_LIMIT_ENV, "10")
    monkeypatch.setenv(firecrawl_gateway.RATE_LIMIT_ENV, "10")
    monkeypatch.setenv(
        firecrawl_gateway.LEDGER_PATH_ENV, str(tmp_path / "copied-ledger.db")
    )
    monkeypatch.setattr(
        firecrawl_gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("copied env reached Firecrawl HTTP"),
    )

    with pytest.raises(
        firecrawl_gateway.FirecrawlBudgetNotConfigured, match="authority file"
    ):
        firecrawl_gateway.search("blocked")
    assert not (tmp_path / "copied-ledger.db").exists()


def test_firecrawl_ledger_bound_to_another_authority_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copied private ledger still cannot be used with a different host capability."""
    configure_firecrawl_runtime(tmp_path, monkeypatch, limit=10)
    second = tmp_path / "second-authority.json"
    authority.initialize_authority_file(
        second,
        {"firecrawl": "test-account-firecrawl"},
        authority_id="different-authority-0001",
    )
    monkeypatch.setenv(authority.AUTHORITY_FILE_ENV, str(second))
    monkeypatch.setattr(
        firecrawl_gateway.requests,
        "post",
        lambda *_a, **_k: pytest.fail("mismatched authority reached HTTP"),
    )

    with pytest.raises(
        firecrawl_gateway.FirecrawlBudgetNotConfigured, match="does not match"
    ):
        firecrawl_gateway.search("blocked")


def test_zoominfo_checks_ledger_binding_even_when_token_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every vendor call rechecks host/account authority before using cached auth."""
    configure_zoominfo_runtime(tmp_path, monkeypatch, limit=10)
    zoominfo._TOKEN_CACHE.access_token = "cached"
    zoominfo._TOKEN_CACHE.expires_at = 99_999_999_999.0
    zoominfo._TOKEN_CACHE.credential_scope = "test-client-id"
    second = tmp_path / "second-authority.json"
    authority.initialize_authority_file(
        second,
        {"zoominfo": "test-account-zoominfo"},
        authority_id="different-authority-0001",
    )
    monkeypatch.setenv(authority.AUTHORITY_FILE_ENV, str(second))
    monkeypatch.setattr(
        zoominfo.requests,
        "post",
        lambda *_a, **_k: pytest.fail("mismatched authority reached ZoomInfo HTTP"),
    )

    with pytest.raises(zoominfo.ZoomInfoConfigurationError, match="does not match"):
        zoominfo.search_contacts("Test District")


def test_disabled_mode_with_any_paid_credential_is_a_startup_failure() -> None:
    """The safe default cannot coexist silently with copied provider secrets."""
    issues = health.runtime_configuration_issues(
        {
            authority.MODE_ENV: "disabled",
            "FIRECRAWL_API_KEY": "configured",
            "FIRECRAWL_RUNTIME_MONTHLY_CALL_LIMIT": "10",
            "FIRECRAWL_RUNTIME_LEDGER_PATH": "/private/firecrawl.db",
        }
    )
    assert any("disabled" in issue and "firecrawl" in issue for issue in issues)


def test_permissive_authority_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capability readable by another local tenant is no capability at all."""
    path = tmp_path / "authority.json"
    authority.initialize_authority_file(
        path,
        {"firecrawl": "account-firecrawl-001"},
        authority_id="test-authority-00000001",
    )
    path.chmod(0o644)
    monkeypatch.setenv(authority.MODE_ENV, "authority")
    monkeypatch.setenv(authority.AUTHORITY_FILE_ENV, str(path))
    with pytest.raises(authority.PaidProviderAuthorityError, match="mode 0600"):
        authority.load_binding("firecrawl")
