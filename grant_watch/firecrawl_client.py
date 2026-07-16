"""Typed, secret-redacting Firecrawl search transport for source research.

Why: nationwide discovery spends API credits and receives runtime-shaped JSON.
This module contains the HTTP boundary, response-size guard, recursive redaction,
and explicit outcome classification so orchestration never stores credentials or
confuses throttling, malformed responses, and truthful zero-result searches.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TypeAlias
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests


FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v1/search"
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


def _retry_after(response: requests.Response) -> float:
    """Parse a numeric Retry-After value without trusting arbitrary header text."""
    value = response.headers.get("Retry-After", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return 0.0
    return max(0.0, seconds)


def _failure(
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


class FirecrawlClient:
    """Make one bounded Firecrawl search call with explicit failure classification."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = FIRECRAWL_SEARCH_URL,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        session: requests.Session | None = None,
    ) -> None:
        """Configure the transport without ever exposing the supplied credential."""
        if not api_key:
            raise ValueError("Firecrawl API key is required")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("Firecrawl transport bounds must be positive")
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._session = session or requests.Session()

    def search_once(self, query: str, result_limit: int) -> SearchOutcome:
        """Execute one call and return a typed result without retrying implicitly."""
        if not query or not 1 <= result_limit <= 5:
            raise ValueError("Firecrawl search requires a query and result_limit 1..5")
        try:
            response = self._session.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query, "limit": result_limit},
                timeout=self._timeout_seconds,
                stream=True,
            )
        except requests.Timeout:
            return _failure("timeout", error_code="requests_timeout", retryable=True)
        except requests.RequestException:
            return _failure("http_error", error_code="requests_error", retryable=True)

        status = response.status_code
        retry_after = _retry_after(response)
        if status in {401, 402, 403}:
            failure = _failure(
                "http_error",
                http_status=status,
                error_code=f"http_{status}",
                retryable=False,
                systemic=True,
            )
            response.close()
            return failure
        if status == 429:
            failure = _failure(
                "rate_limited",
                http_status=status,
                retry_after_seconds=retry_after,
                error_code="http_429",
                retryable=True,
            )
            response.close()
            return failure
        if status >= 500:
            failure = _failure(
                "http_error",
                http_status=status,
                error_code=f"http_{status}",
                retryable=True,
            )
            response.close()
            return failure
        if status >= 400:
            failure = _failure(
                "http_error",
                http_status=status,
                error_code=f"http_{status}",
                retryable=False,
            )
            response.close()
            return failure
        try:
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65_536):
                body.extend(chunk)
                if len(body) > self._max_response_bytes:
                    return _failure(
                        "oversized_response",
                        http_status=status,
                        error_code="response_too_large",
                        retryable=False,
                    )
            raw_payload: object = json.loads(body)
            payload = redact_json(raw_payload, secret_values=(self._api_key,))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return _failure(
                "malformed_response",
                http_status=status,
                error_code="invalid_json_shape",
                retryable=False,
            )
        except requests.RequestException:
            return _failure(
                "http_error",
                error_code="response_stream_error",
                retryable=True,
            )
        finally:
            response.close()
        if not isinstance(payload, dict):
            return _failure(
                "malformed_response",
                http_status=status,
                error_code="root_not_object",
                retryable=False,
            )
        raw_results = payload.get("data")
        if payload.get("success") is not True or not isinstance(raw_results, list):
            return _failure(
                "malformed_response",
                http_status=status,
                error_code="missing_success_data",
                retryable=False,
            )
        if len(raw_results) > result_limit:
            return _failure(
                "malformed_response",
                http_status=status,
                error_code="result_limit_exceeded",
                retryable=False,
            )
        results: list[SearchResultEvidence] = []
        for rank, item in enumerate(raw_results, start=1):
            if not isinstance(item, dict):
                return _failure(
                    "malformed_response",
                    http_status=status,
                    error_code="result_not_object",
                    retryable=False,
                )
            results.append(SearchResultEvidence(rank=rank, metadata=item))
        metadata = {key: value for key, value in payload.items() if key != "data"}
        return SearchOutcome(
            outcome="success" if results else "zero_results",
            http_status=status,
            retry_after_seconds=retry_after,
            response_sha256=canonical_json_hash(payload),
            response_metadata=metadata,
            results=tuple(results),
            error_code="",
            sanitized_error="",
            retryable=False,
            systemic=False,
        )
