"""Tests for secret-redacted Firecrawl evidence after account-metered HTTP."""

from __future__ import annotations

import json

import pytest

from grant_watch.firecrawl_client import (
    REDACTED,
    canonical_json_hash,
    failure_outcome,
    redact_json,
    search_outcome_from_payload,
)


def test_redact_json_removes_nested_secrets_and_url_query_values() -> None:
    """Secret-bearing keys and URL parameters never survive recursive redaction."""
    value = redact_json(
        {
            "authorization": "Bearer private",
            "nested": [{"apiKey": "private", "safe": "kept\non one line"}],
            "url": "https://example.gov/bids?api_key=private&state=CA",
        }
    )
    encoded = REDACTED.replace("[", "%5B").replace("]", "%5D")
    assert value == {
        "authorization": REDACTED,
        "nested": [{"apiKey": REDACTED, "safe": "kept on one line"}],
        "url": f"https://example.gov/bids?api_key={encoded}&state=CA",
    }
    assert "private" not in json.dumps(value)


def test_outcome_redacts_exact_credential_echoed_anywhere_in_payload() -> None:
    """The known API-key value cannot survive in text, paths, or fragments."""
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

    outcome = search_outcome_from_payload(payload, 1, secret_values=(secret,))

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

    outcome = search_outcome_from_payload(payload, 2)

    assert outcome.outcome == "success"
    assert [result.rank for result in outcome.results] == [1, 2]
    assert outcome.results[0].metadata["title"] == "Bids and RFPs"
    assert outcome.results[0].metadata["metadata"] == {
        "token": REDACTED,
        "kind": "official",
    }
    assert outcome.response_metadata == {"success": True, "creditsUsed": 1}
    assert len(outcome.response_sha256) == 64


def test_zero_results_is_successful_evidence_not_a_failure() -> None:
    """An empty result list receives its own truthful terminal outcome."""
    payload = {"success": True, "data": []}
    outcome = search_outcome_from_payload(payload, 5)
    assert outcome.outcome == "zero_results"
    assert outcome.results == ()
    assert outcome.response_sha256 == canonical_json_hash(payload)


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (["not", "object"], "root_not_object"),
        ({"success": False, "data": []}, "missing_success_data"),
        ({"success": True, "data": ["bad"]}, "result_not_object"),
        (
            {"success": True, "data": [{"url": "1"}, {"url": "2"}]},
            "result_limit_exceeded",
        ),
    ],
)
def test_malformed_payloads_never_become_search_results(
    payload: object, error_code: str
) -> None:
    """Malformed metered payloads fail explicitly without retry loops."""
    outcome = search_outcome_from_payload(payload, 1)
    assert outcome.outcome == "malformed_response"
    assert outcome.error_code == error_code
    assert not outcome.retryable


def test_unsupported_json_shape_becomes_sanitized_failure() -> None:
    """A value redaction cannot serialize is rejected without leaking its repr."""
    outcome = search_outcome_from_payload(
        {"success": True, "data": [{"bad": object()}]}, 1
    )
    assert outcome.error_code == "invalid_json_shape"
    assert outcome.response_metadata == {}


def test_failure_outcome_retains_only_safe_classification() -> None:
    """Gateway exceptions become metadata-only batch evidence."""
    result = failure_outcome(
        "rate_limited",
        retry_after_seconds=7,
        error_code="account_rate_limited",
        retryable=True,
    )
    assert result.outcome == "rate_limited"
    assert result.retry_after_seconds == 7
    assert result.retryable is True
    assert result.response_metadata == {}
    assert result.response_sha256 == ""
