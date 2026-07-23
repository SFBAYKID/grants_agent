"""Block Kit presentation tests: truth, safety, limits, links, and actions."""

from __future__ import annotations

from dataclasses import replace

from grant_watch.campaign import card
from grant_watch.campaign.routing import Route, RoutingReason
from grant_watch.campaign.snapshot import FrozenSnapshot
from tests.test_rich_snapshot import _draft


def _snapshot(**changes: object) -> FrozenSnapshot:
    """Create one frozen fixture without a database."""
    draft = replace(_draft(), **changes)  # type: ignore[arg-type]
    draft = replace(draft, fallback_text=card.fallback_text(draft))
    return FrozenSnapshot("a" * 32, 1, "2026-07-22T18:00:00+00:00", draft)


def test_gold_card_has_accessible_truthful_sections_and_actions() -> None:
    """The full card labels award, spend window, evidence links, and safe actions."""
    rendered = card.render(_snapshot())
    encoded = str(rendered.blocks)
    assert rendered.text.startswith("GOLD:")
    assert "$500,000 SVPP funding award" in rendered.text
    assert "Spend window" in rendered.text
    assert "Ask Persequor to draft" in encoded
    assert "Not relevant" in encoded
    assert "View exact award record" in encoded
    assert "Official website" in encoded
    assert "Contact evidence" in encoded
    assert "Open Salesforce" in encoded
    assert "Send email" not in encoded
    assert "Anybody want to talk?" not in encoded
    assert "remaining" not in encoded.lower()


def test_research_needed_card_offers_no_active_draft_action() -> None:
    """An ambiguous-CRM research card must NOT render the Persequor draft button, must
    show the ambiguity banner, and must claim no relationship/net-new (Chase 2026-07-23)."""
    rendered = card.render(
        _snapshot(
            card_mode="research_needed",
            sf_display_text="Possible Salesforce matches—review before outreach.",
            route=Route(RoutingReason.TERRITORY, "U01DFJWQQJ3"),
        )
    )
    encoded = str(rendered.blocks)
    assert "Ask Persequor to draft" not in encoded
    assert "Not relevant" in encoded
    assert "resolve it before drafting outreach" in encoded
    assert "Possible Salesforce matches—review before outreach" in rendered.text
    assert "territory owner" in encoded  # never "relationship owner"
    assert "relationship owner" not in encoded
    assert "net-new" not in rendered.text.lower()
    assert (
        "Resolve the Salesforce match before drafting outreach" in rendered.text
    )


def test_draft_ready_salesforce_line_has_no_double_period() -> None:
    """The Salesforce fallback line renders exactly one period, never '…present..'."""
    rendered = card.render(
        _snapshot(sf_display_text="Exact Account match. Open Opportunity present.")
    )
    assert "present.." not in rendered.text
    assert "Exact Account match. Open Opportunity present." in rendered.text


def test_platinum_and_unassigned_routes_render_honestly() -> None:
    """Presentation tier and nationwide unassigned state are explicit, never guessed."""
    rendered = card.render(
        _snapshot(tier="platinum", route=Route(RoutingReason.UNASSIGNED))
    )
    assert "PLATINUM" in str(rendered.blocks)
    assert "Unassigned territory" in rendered.text
    assert "&lt;@" not in str(rendered.blocks)


def test_verified_route_preserves_only_the_safe_slack_mention() -> None:
    """A roster/membership-validated Slack id remains a functional mention."""
    rendered = card.render(_snapshot())
    assert "<@U01DFJWQQJ3>" in rendered.blocks[1]["text"]["text"]


def test_untrusted_markup_is_escaped_and_action_values_are_opaque() -> None:
    """Source text cannot inject mentions/links and action values contain no PII."""
    rendered = card.render(
        _snapshot(entity_name="<!channel> <https://evil.test|click>")
    )
    encoded = str(rendered.blocks)
    assert "<!channel>" not in encoded
    assert "&lt;!channel&gt;" in encoded
    actions = rendered.blocks[-1]["elements"]
    assert {button["value"] for button in actions} == {"a" * 32}
    assert "@" not in actions[0]["value"]
    assert "http" not in actions[0]["value"]


def test_unsafe_links_are_omitted() -> None:
    """Credentials, secrets, non-HTTPS links, and Slack markup never become links."""
    rendered = card.render(
        _snapshot(
            official_website="http://district.test",
            contact_evidence_url="https://user:pass@district.test/staff",
            sf_open_link="https://sf.test/x?access_token=secret",
            award_url="https://award.test/x|<bad>",
        )
    )
    assert not any(block["type"] == "context" for block in rendered.blocks)


def test_block_and_fallback_lengths_are_bounded() -> None:
    """Extremely long source fields stay inside Slack's documented limits."""
    rendered = card.render(
        _snapshot(
            entity_name="X" * 10000,
            contact_name="Y" * 10000,
            sf_display_text="Z" * 10000,
            fallback_text="F" * 10000,
        )
    )
    assert len(rendered.text) <= card.MAX_FALLBACK
    for block in rendered.blocks:
        if "text" in block and isinstance(block["text"], dict):
            assert len(block["text"]["text"]) <= card.MAX_SECTION
        for field in block.get("fields", []):
            assert len(field["text"]) <= card.MAX_FIELD


def test_salesforce_section_and_link_are_absent_without_evidence() -> None:
    """A complete CRM no-match card does not imply a relationship or show a CRM URL."""
    rendered = card.render(
        _snapshot(
            sf_display_text="", sf_open_link="", sf_lookup_status="complete_no_match"
        )
    )
    encoded = str(rendered.blocks)
    assert "*Salesforce*" not in encoded
    assert "Open Salesforce" not in encoded


def test_month_precision_never_invents_the_first_day() -> None:
    """Month-only evidence renders as a month, never a normalized exact day."""
    rendered = card.render(
        _snapshot(award_date="2026-06-01", award_date_precision="month")
    )
    assert "Federal funds obligated: June 2026" in rendered.text
    assert "June 1, 2026" not in rendered.text
    assert "June 1, 2026" not in str(rendered.blocks)


def test_event_date_label_distinguishes_obligation_from_announcement() -> None:
    """The card labels the exact frozen event semantics: an obligation is 'Federal funds
    obligated' and an announcement is 'Award announced' — never a bare 'Awarded'."""
    obligated = card.render(_snapshot(event_type="award_obligated"))
    announced = card.render(_snapshot(event_type="award_announced"))
    assert "Federal funds obligated" in str(obligated.blocks)
    assert "Federal funds obligated:" in obligated.text
    assert "Awarded" not in obligated.text
    assert "Award announced" in str(announced.blocks)
    assert "Award announced:" in announced.text


def test_context_link_element_respects_aggregate_limit() -> None:
    """Several individually safe long URLs cannot overflow one context element."""
    long_path = "x" * 1900
    rendered = card.render(
        _snapshot(
            official_website=f"https://district.test/{long_path}",
            contact_evidence_url=f"https://district.test/staff/{long_path}",
            sf_open_link=f"https://sf.test/{long_path}",
            award_url=f"https://award.test/{long_path}",
        )
    )
    contexts = [block for block in rendered.blocks if block["type"] == "context"]
    assert contexts
    assert len(contexts[0]["elements"][0]["text"]) <= card.MAX_CONTEXT
