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
