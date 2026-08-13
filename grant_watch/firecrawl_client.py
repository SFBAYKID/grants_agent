"""Typed, secret-redacting Firecrawl search evidence for source research.

Nationwide discovery receives runtime-shaped JSON, but its HTTP authority lives only
in :mod:`grant_watch.enrich.firecrawl_gateway`. This module converts an already
account-metered response into secret-free immutable batch evidence. Keeping transport
out of this module makes it impossible for discovery to bypass the shared account
ceiling, proactive rate slot, provider backoff, or indeterminate-request ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TypeAlias
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

MAX_RESPONSE_BYTES = 2_000_000
SECRET_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|secret|token)", re.I
)
REDACTED = "[REDACTED]"

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class SearchResultEvidence:
    """One ranked Firecrawl result with its complete redacted metadata."""

    rank: int
    metadata: dict[str, JsonValue]


@dataclass(frozen=True)
class SearchOutcome:
    """One classified Firecrawl call outcome safe for durable persistence."""

    outcome: str
    http_status: int
    retry_after_seconds: float
    response_sha256: str
    response_metadata: dict[str, JsonValue]
    results: tuple[SearchResultEvidence, ...]
    error_code: str
    sanitized_error: str
    retryable: bool
    systemic: bool


def _clean_text(value: str) -> str:
    """Collapse physical line breaks so logs and JSONL remain one record per line."""
    return " ".join(value.split())


def _replace_secret_values(value: str, secret_values: tuple[str, ...]) -> str:
    """Replace exact credentials and their URL-encoded forms in arbitrary text."""
    redacted = value
    for secret in secret_values:
        if not secret:
            continue
        redacted = redacted.replace(secret, REDACTED)
        encoded = quote(secret, safe="")
        if encoded != secret:
            redacted = redacted.replace(encoded, REDACTED)
            redacted = redacted.replace(encoded.lower(), REDACTED)
    return redacted


def _redact_url(value: str) -> str:
    """Redact secret-bearing URL query values while preserving other evidence."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _clean_text(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _clean_text(value)
    query = [
        (key, REDACTED if SECRET_KEY_PATTERN.search(key) else item_value)
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def redact_json(
    value: object,
    key_hint: str = "",
    *,
    secret_values: tuple[str, ...] = (),
) -> JsonValue:
    """Convert runtime JSON to typed JSON while removing keys and exact secrets."""
    if SECRET_KEY_PATTERN.search(key_hint):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        cleaned = _replace_secret_values(_clean_text(value), secret_values)
        return (
            _redact_url(cleaned)
            if cleaned.startswith(("http://", "https://"))
            else cleaned
        )
    if isinstance(value, list):
        return [redact_json(item, secret_values=secret_values) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, JsonValue] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("Firecrawl JSON object contains a non-string key")
            safe_key = _replace_secret_values(raw_key, secret_values)
            if safe_key in redacted:
                raise ValueError("Firecrawl redaction produced a duplicate JSON key")
            redacted[safe_key] = redact_json(
                item, safe_key, secret_values=secret_values
            )
        return redacted
    raise ValueError(f"unsupported Firecrawl JSON value: {type(value).__name__}")


def canonical_json_hash(value: JsonValue) -> str:
    """Hash one redacted JSON value using a deterministic representation."""
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def failure_outcome(
    outcome: str,
    *,
    http_status: int = 0,
    retry_after_seconds: float = 0.0,
    error_code: str,
    retryable: bool,
    systemic: bool = False,
) -> SearchOutcome:
    """Build a metadata-only failure that cannot leak an exception or header."""
    return SearchOutcome(
        outcome=outcome,
        http_status=http_status,
        retry_after_seconds=retry_after_seconds,
        response_sha256="",
        response_metadata={},
        results=(),
        error_code=error_code,
        sanitized_error=error_code,
        retryable=retryable,
        systemic=systemic,
    )


def search_outcome_from_payload(
    payload: object,
    result_limit: int,
    *,
    secret_values: tuple[str, ...] = (),
) -> SearchOutcome:
    """Convert one metered successful HTTP payload into immutable safe evidence."""
    if not 1 <= result_limit <= 5:
        raise ValueError("Firecrawl result_limit must be from 1 to 5")
    try:
        redacted = redact_json(payload, secret_values=secret_values)
    except ValueError:
        return failure_outcome(
            "malformed_response",
            http_status=200,
            error_code="invalid_json_shape",
            retryable=False,
        )
    if not isinstance(redacted, dict):
        return failure_outcome(
            "malformed_response",
            http_status=200,
            error_code="root_not_object",
            retryable=False,
        )
    raw_results = redacted.get("data")
    if redacted.get("success") is not True or not isinstance(raw_results, list):
        return failure_outcome(
            "malformed_response",
            http_status=200,
            error_code="missing_success_data",
            retryable=False,
        )
    if len(raw_results) > result_limit:
        return failure_outcome(
            "malformed_response",
            http_status=200,
            error_code="result_limit_exceeded",
            retryable=False,
        )
    results: list[SearchResultEvidence] = []
    for rank, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            return failure_outcome(
                "malformed_response",
                http_status=200,
                error_code="result_not_object",
                retryable=False,
            )
        results.append(SearchResultEvidence(rank=rank, metadata=item))
    metadata = {key: value for key, value in redacted.items() if key != "data"}
    return SearchOutcome(
        outcome="success" if results else "zero_results",
        http_status=200,
        retry_after_seconds=0.0,
        response_sha256=canonical_json_hash(redacted),
        response_metadata=metadata,
        results=tuple(results),
        error_code="",
        sanitized_error="",
        retryable=False,
        systemic=False,
    )
