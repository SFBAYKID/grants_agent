"""Deterministic data-source coverage answers and contextual follow-up tests."""

from __future__ import annotations

import pytest

from grant_watch.slack import conversation


def _forbid_model() -> object:
    """Fail if a deterministic source question reaches the language model."""
    raise AssertionError("source coverage must not become a model-authored lead search")


def test_direct_data_source_question_lists_actual_integrations(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant names its real feeds and distinguishes Salesforce from funding data."""
    monkeypatch.setattr(conversation, "Anthropic", _forbid_model)
    result = conversation.respond("What data sources do you use?", None)
    reply = result["reply"]
    assert "USAspending" in reply and "Grants.gov" in reply and "SAM.gov" in reply
    assert "California Grants Portal" in reply
    assert "Salesforce is my CRM cross-check, not a funding source" in reply


def test_florida_followup_stays_about_source_coverage(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """“Any on Florida?” after a source answer cannot start a lead search."""
    monkeypatch.setattr(conversation, "Anthropic", _forbid_model)
    result = conversation.respond(
        "Any on Florida?", None,
        thread_context=[
            "rep: What data sources do you use?",
            "Grant: Here are the data sources that feed my leads.",
        ])
    reply = result["reply"]
    assert "current coverage for *Florida*" in reply
    assert "Dedicated Florida feed:* none integrated yet" in reply
    assert "local coverage is not comprehensive" in reply
    assert "How many" not in reply and "top 5" not in reply


def test_explicit_florida_lead_request_is_not_misclassified() -> None:
    """A real lead search remains distinct from a source-coverage question."""
    assert not conversation._is_source_coverage_request(
        "Show me Florida school funding leads", [])


def test_salesforce_preview_with_verified_sources_is_not_source_coverage() -> None:
    """A CRM action cannot be diverted merely because it asks to include sources."""
    assert not conversation._is_source_coverage_request(
        "Use that exact person. Prepare the Salesforce Lead preview with every "
        "verified organization field and source.",
        ["Grant: I found that exact person on LinkedIn."],
    )


def test_california_reports_only_its_integrated_state_feed() -> None:
    """State-specific copy names a dedicated feed only where one is registered."""
    reply = conversation._source_coverage_reply("What sources cover California?")
    assert "Dedicated California feed:* California Grants Portal" in reply
    assert "OregonBuys" not in reply and "Washington WEBS" not in reply
