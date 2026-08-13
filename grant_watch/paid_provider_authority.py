"""Host-local capability required before Grant may call a paid-data provider.

An API credential copied to a laptop must not copy spend authority with it.  Runtime
therefore requires an owner-only capability file outside the deploy tree and binds
each standalone provider ledger to the random authority and vendor-account scope in
that file.  Local SQLite cannot prove that a second machine lacks a credential;
credential rotation/revocation is still a required operational cutover step.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Final

MODE_ENV: Final = "GRANT_PAID_PROVIDER_MODE"
AUTHORITY_FILE_ENV: Final = "GRANT_PAID_PROVIDER_AUTHORITY_FILE"
AUTHORITY_SCHEMA_VERSION: Final = 1
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_PROVIDERS = frozenset({"firecrawl", "zoominfo"})


class PaidProviderAuthorityError(RuntimeError):
    """A paid-provider call lacks a valid, host-local authority capability."""


@dataclass(frozen=True)
class ProviderBinding:
    """Stable identity binding one host authority to one vendor account."""

    provider: str
    authority_id: str
    account_scope_id: str


def _identifier(value: object, label: str) -> str:
    """Return one bounded opaque identifier or reject malformed authority data."""
    normalized = str(value or "").strip()
    if _IDENTIFIER_RE.fullmatch(normalized) is None:
        raise PaidProviderAuthorityError(f"authority {label} is malformed")
    return normalized


def _private_file(path: Path) -> None:
    """Require an owner-only regular file reached without a symbolic link."""
    if path.is_symlink() or not path.is_file():
        raise PaidProviderAuthorityError(
            "paid-provider authority file is missing or is not a regular file"
        )
    stat = path.stat()
    if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise PaidProviderAuthorityError(
            "paid-provider authority file must be owned by this user and mode 0600"
        )


def _authority_path(values: Mapping[str, str]) -> Path:
    """Return the configured absolute capability path without creating anything."""
    raw = str(values.get(AUTHORITY_FILE_ENV, "")).strip()
    if not raw:
        raise PaidProviderAuthorityError(f"{AUTHORITY_FILE_ENV} is not configured")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise PaidProviderAuthorityError(f"{AUTHORITY_FILE_ENV} must be absolute")
    return path


def load_binding(
    provider: str, environ: Mapping[str, str] | None = None
) -> ProviderBinding:
    """Load and validate one provider binding from the private capability file."""
    values = environ if environ is not None else os.environ
    if provider not in _PROVIDERS:
        raise ValueError(f"unsupported paid provider {provider!r}")
    mode = str(values.get(MODE_ENV, "disabled")).strip().lower() or "disabled"
    if mode != "authority":
        raise PaidProviderAuthorityError(
            f"{MODE_ENV} must be 'authority' before paid-provider access"
        )
    path = _authority_path(values)
    _private_file(path)
    try:
        raw = path.read_bytes()
        if len(raw) > 16_384:
            raise PaidProviderAuthorityError(
                "paid-provider authority file is oversized"
            )
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaidProviderAuthorityError(
            "paid-provider authority file is unreadable or malformed"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PaidProviderAuthorityError(
            "paid-provider authority schema is missing or unsupported"
        )
    providers = payload.get("providers")
    if not isinstance(providers, dict) or not isinstance(providers.get(provider), dict):
        raise PaidProviderAuthorityError(
            f"paid-provider authority does not allow {provider}"
        )
    provider_payload = providers[provider]
    return ProviderBinding(
        provider=provider,
        authority_id=_identifier(payload.get("authority_id"), "authority_id"),
        account_scope_id=_identifier(
            provider_payload.get("account_scope_id"),
            f"{provider}.account_scope_id",
        ),
    )


def require_call_authority(
    provider: str, credential_envs: tuple[str, ...]
) -> ProviderBinding:
    """Require the host capability and every provider credential before HTTP."""
    binding = load_binding(provider)
    missing = [name for name in credential_envs if not os.environ.get(name, "").strip()]
    if missing:
        raise PaidProviderAuthorityError(
            f"{provider} credential is incomplete; missing {', '.join(missing)}"
        )
    return binding


def configuration_issues(values: Mapping[str, str]) -> list[str]:
    """Return fail-closed startup issues without reading secret values into output."""
    mode = str(values.get(MODE_ENV, "disabled")).strip().lower() or "disabled"
    credentials = {
        "firecrawl": bool(str(values.get("FIRECRAWL_API_KEY", "")).strip()),
        "zoominfo": bool(
            str(values.get("ZOOMINFO_CLIENT_ID", "")).strip()
            or str(values.get("ZOOMINFO_CLIENT_SECRET", "")).strip()
        ),
    }
    issues: list[str] = []
    if mode not in {"disabled", "authority"}:
        issues.append(f"{MODE_ENV} must be disabled or authority")
        return issues
    if mode == "disabled":
        enabled = sorted(
            provider for provider, present in credentials.items() if present
        )
        if enabled:
            issues.append(
                f"{MODE_ENV}=disabled but paid-provider credentials are configured: "
                + ", ".join(enabled)
            )
        return issues
    path_raw = str(values.get(AUTHORITY_FILE_ENV, "")).strip()
    if not path_raw:
        issues.append(f"{AUTHORITY_FILE_ENV} is required in authority mode")
    elif not Path(path_raw).expanduser().is_absolute():
        issues.append(f"{AUTHORITY_FILE_ENV} must be absolute")
    else:
        providers = [provider for provider, present in credentials.items() if present]
        # Validate the base file even before a credential is installed. A malformed
        # capability must never become latent authority waiting for an env copy.
        providers_to_check = providers or ["firecrawl"]
        try:
            load_binding(providers_to_check[0], values)
        except PaidProviderAuthorityError as exc:
            if providers:
                issues.append(str(exc))
            else:
                # A file may intentionally authorize only ZoomInfo. Try the other
                # reviewed provider before declaring the base capability malformed.
                try:
                    load_binding("zoominfo", values)
                except PaidProviderAuthorityError:
                    issues.append(str(exc))
        for provider in providers[1:]:
            try:
                load_binding(provider, values)
            except PaidProviderAuthorityError as exc:
                issues.append(str(exc))
    return issues


def initialize_authority_file(
    path: Path,
    provider_scopes: dict[str, str],
    *,
    authority_id: str | None = None,
) -> str:
    """Explicitly create one private capability file without replacing a target.

    This is an operator/bootstrap API, never called by provider runtime.  It returns
    the generated authority id so deployment records can bind ledgers to it.
    """
    destination = path.expanduser()
    if not destination.is_absolute():
        raise PaidProviderAuthorityError("authority destination must be absolute")
    if destination.exists() or destination.is_symlink():
        raise PaidProviderAuthorityError("refusing to replace an authority file")
    if not provider_scopes or set(provider_scopes) - _PROVIDERS:
        raise PaidProviderAuthorityError("authority providers are empty or unsupported")
    selected_id = _identifier(
        authority_id or f"auth-{uuid.uuid4().hex}", "authority_id"
    )
    providers = {
        provider: {
            "account_scope_id": _identifier(scope, f"{provider}.account_scope_id")
        }
        for provider, scope in sorted(provider_scopes.items())
    }
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o600)
    try:
        encoded = (
            json.dumps(
                {
                    "schema_version": AUTHORITY_SCHEMA_VERSION,
                    "authority_id": selected_id,
                    "providers": providers,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return selected_id
