"""Tests for the bounded secret-redacting Firecrawl HTTP boundary."""

from __future__ import annotations

import json

import pytest
import requests

from grant_watch.firecrawl_client import (
    REDACTED,
    FirecrawlClient,
    canonical_json_hash,
    redact_json,
)


class FakeSession:
    """Return one prepared response or exception while recording request metadata."""

    def __init__(self, response: requests.Response | requests.RequestException) -> None:
        """Store the deterministic transport result used by one test."""
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> requests.Response:
        """Record one POST and return/raise the prepared transport result."""
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, requests.RequestException):
            raise self.response
        return self.response


def _response(
    status: int = 200,
    payload: object = None,
    *,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Build a real requests response with deterministic JSON or raw bytes."""
    response = requests.Response()
    response.status_code = status
    response._content = raw if raw is not None else json.dumps(payload).encode()
    response._content_consumed = True
    response.headers.update(headers or {})
    return response


def test_redact_json_removes_nested_secrets_and_url_query_values() -> None:
    """Secret-bearing keys and URL parameters never survive recursive redaction."""
    value = redact_json(
        {
            "authorization": "Bearer private",
            "nested": [{"apiKey": "private", "safe": "kept\non one line"}],
            "url": "https://example.gov/bids?api_key=private&state=CA",
        }
    )
    assert value == {
        "authorization": REDACTED,
        "nested": [{"apiKey": REDACTED, "safe": "kept on one line"}],
        "url": f"https://example.gov/bids?api_key={REDACTED.replace('[', '%5B').replace(']', '%5D')}&state=CA",
    }
    assert "private" not in json.dumps(value)


def test_client_redacts_exact_credential_echoed_in_text_and_url_components() -> None:
    """The known API-key value cannot survive in headers, paths, or fragments."""
    secret = "fc-live-secret"
    payload = {
        "success": True,
        "data": [
            {
                "description": f"Bearer {secret}",
                "url": f"https://user:{secret}@example.gov/{secret}#{secret}",
            }
        ],
    }
    outcome = FirecrawlClient(
        secret, session=FakeSession(_response(payload=payload))
    ).search_once("query", 1)
    serialized = json.dumps(outcome.__dict__, default=str)
    assert secret not in serialized
    assert serialized.count(REDACTED) >= 2


def test_success_preserves_full_ranked_metadata_and_hashes_redacted_payload() -> None:
    """Successful results retain nested metadata and duplicate URLs by rank."""
    payload = {
        "success": True,
        "creditsUsed": 1,
        "data": [
            {
                "url": "https://example.gov/bids",
                "title": "Bids\nand RFPs",
                "description": "First",
                "metadata": {"token": "private", "kind": "official"},
            },
            {
                "url": "https://example.gov/bids",
                "title": "Duplicate rank",
                "description": "Second",
            },
        ],
    }
    session = FakeSession(_response(payload=payload))
    outcome = FirecrawlClient("fc-test-secret", session=session).search_once(
        "test query", 2
    )
    assert outcome.outcome == "success"
    assert [result.rank for result in outcome.results] == [1, 2]
    assert outcome.results[0].metadata["title"] == "Bids and RFPs"
    assert outcome.results[0].metadata["metadata"] == {
        "token": REDACTED,
        "kind": "official",
    }
    assert outcome.response_metadata == {"success": True, "creditsUsed": 1}
    assert len(outcome.response_sha256) == 64
    assert "fc-test-secret" not in json.dumps(outcome.__dict__, default=str)
    assert session.calls[0]["json"] == {"query": "test query", "limit": 2}


def test_zero_results_is_successful_evidence_not_a_failure() -> None:
    """An empty Firecrawl result list receives its own truthful terminal outcome."""
    outcome = FirecrawlClient(
        "key", session=FakeSession(_response(payload={"success": True, "data": []}))
    ).search_once("query", 5)
    assert outcome.outcome == "zero_results"
    assert outcome.results == ()
    assert outcome.response_sha256 == canonical_json_hash({"success": True, "data": []})


@pytest.mark.parametrize(
    ("status", "headers", "outcome", "retryable", "systemic"),
    [
        (401, {}, "http_error", False, True),
        (402, {}, "http_error", False, True),
        (403, {}, "http_error", False, True),
        (429, {"Retry-After": "7"}, "rate_limited", True, False),
        (500, {}, "http_error", True, False),
        (404, {}, "http_error", False, False),
    ],
)
def test_http_failures_are_distinct_and_metadata_only(
    status: int,
    headers: dict[str, str],
    outcome: str,
    retryable: bool,
    systemic: bool,
) -> None:
    """HTTP classes retain retry/systemic semantics without response bodies."""
    result = FirecrawlClient(
        "key",
        session=FakeSession(_response(status, {"secret": "private"}, headers=headers)),
    ).search_once("query", 5)
    assert result.outcome == outcome
    assert result.retryable is retryable
    assert result.systemic is systemic
    assert result.response_metadata == {}
    assert result.response_sha256 == ""
    if status == 429:
        assert result.retry_after_seconds == 7


@pytest.mark.parametrize(
    ("payload", "raw", "error_code"),
    [
        (None, b"not json", "invalid_json_shape"),
        (["not", "object"], None, "root_not_object"),
        ({"success": False, "data": []}, None, "missing_success_data"),
        ({"success": True, "data": ["bad"]}, None, "result_not_object"),
        (
            {"success": True, "data": [{"url": "1"}, {"url": "2"}]},
            None,
            "result_limit_exceeded",
        ),
    ],
)
def test_malformed_responses_never_become_search_results(
    payload: object, raw: bytes | None, error_code: str
) -> None:
    """Malformed JSON and response shapes fail explicitly without retry loops."""
    outcome = FirecrawlClient(
        "key", session=FakeSession(_response(payload=payload, raw=raw))
    ).search_once("query", 1)
    assert outcome.outcome == "malformed_response"
    assert outcome.error_code == error_code
    assert not outcome.retryable


def test_timeout_and_oversized_response_have_separate_outcomes() -> None:
    """Transport timeouts may retry while oversized evidence stops safely."""
    timeout = FirecrawlClient(
        "key", session=FakeSession(requests.Timeout("private detail"))
    ).search_once("query", 1)
    assert timeout.outcome == "timeout"
    assert timeout.retryable
    oversized = FirecrawlClient(
        "key",
        max_response_bytes=5,
        session=FakeSession(_response(payload={"success": True, "data": []})),
    ).search_once("query", 1)
    assert oversized.outcome == "oversized_response"
    assert not oversized.retryable
