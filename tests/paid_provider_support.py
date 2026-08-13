"""Private paid-provider authority fixtures shared by offline integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch import paid_provider_authority as authority


def configure_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *providers: str,
) -> dict[str, authority.ProviderBinding]:
    """Create and configure one test-local host capability for named providers."""
    scopes = {provider: f"test-account-{provider}" for provider in providers}
    path = tmp_path / "paid-provider-authority.json"
    authority.initialize_authority_file(
        path,
        scopes,
        authority_id="test-authority-00000001",
    )
    monkeypatch.setenv(authority.MODE_ENV, "authority")
    monkeypatch.setenv(authority.AUTHORITY_FILE_ENV, str(path))
    return {provider: authority.load_binding(provider) for provider in providers}


def configure_firecrawl_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    limit: int = 10,
) -> Path:
    """Create and configure one private standalone Firecrawl runtime ledger."""
    from grant_watch.enrich import firecrawl_gateway

    authority_path = tmp_path / "paid-provider-authority.json"
    if not authority_path.exists():
        configure_authority(tmp_path, monkeypatch, "firecrawl")
    ledger_path = tmp_path / "firecrawl-runtime-ledger.db"
    if not ledger_path.exists():
        firecrawl_gateway.initialize_empty_ledger(ledger_path)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-secret")
    monkeypatch.setenv(firecrawl_gateway.MONTHLY_LIMIT_ENV, str(limit))
    # Keep ordinary unit tests fast. Dedicated limiter tests select a production-like
    # interval and exercise independent processes explicitly.
    monkeypatch.setenv(firecrawl_gateway.RATE_LIMIT_ENV, "10")
    monkeypatch.setattr(firecrawl_gateway, "_configured_rate_interval", lambda: 0.0)
    monkeypatch.setenv(firecrawl_gateway.LEDGER_PATH_ENV, str(ledger_path))
    return ledger_path


def configure_zoominfo_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    limit: int = 10,
) -> Path:
    """Create and configure one private standalone ZoomInfo account ledger."""
    from grant_watch.enrich import zoominfo_credits

    authority_path = tmp_path / "paid-provider-authority.json"
    if not authority_path.exists():
        configure_authority(tmp_path, monkeypatch, "zoominfo")
    ledger_path = tmp_path / "zoominfo-account-ledger.db"
    if not ledger_path.exists():
        zoominfo_credits.initialize_empty_ledger(ledger_path)
    monkeypatch.setenv("ZOOMINFO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ZOOMINFO_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("ZOOMINFO_MONTHLY_CREDITS", str(limit))
    monkeypatch.setenv(zoominfo_credits.LEDGER_PATH_ENV, str(ledger_path))
    return ledger_path
