"""Tests for the daily card's Block Kit restyle and the rich→daily fallback rule.

The restyle (Chase 2026-08-05) must add PRESENTATION only: every block carries a
substring of the plain `text` the card already posts, so the two can never disagree
about a fact, and no evidence-gated element (contact, website, Salesforce, action
button) can appear on this path. The fallback classifier decides which rich outcomes
hand the tick to the legacy card — the cases where nothing was posted, no Slack call
was attempted, and the rich path provably cannot post later today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drip_support import SlackClient, mk_lead
from grant_watch import db
from grant_watch.campaign import delivery, pacing
from grant_watch.slack import drip
from grant_watch.slack.drip_card import render_blocks


def _block_texts(blocks: list[dict[str, object]]) -> list[str]:
    """Flatten every human-visible string out of a Block Kit list."""
    texts: list[str] = []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, dict):
            texts.append(str(text.get("text", "")))
        for element in block.get("elements", []) if "elements" in block else []:
            if isinstance(element, dict):
                texts.append(str(element.get("text", "")))
    return texts


# ------------------------------------------------------------- render_blocks
def test_all_pieces_render_in_order_with_the_kind_header() -> None:
    """Header, sentence, routing, and source land as separate blocks, in order."""
    blocks = render_blocks(
        "nugget",
        "Bethlehem Area School District has a verified $500,000 SVPP funding award.",
        "\n\n<@U08C1NBH875> — Pennsylvania is your territory. Want me to find the right contact?",
        "\n\n<https://example.test/award|View the source record>",
    )
    assert [b["type"] for b in blocks] == ["header", "section", "section", "context"]
    assert blocks[0]["text"]["text"] == "GOLD · Verified award"
    assert blocks[1]["text"]["text"].startswith("Bethlehem Area School District")
    assert "<@U08C1NBH875>" in blocks[2]["text"]["text"]
    assert "View the source record" in blocks[3]["elements"][0]["text"]


def test_empty_routing_and_source_drop_their_blocks() -> None:
    """An inferred state or unsafe URL yields no block, never a placeholder."""
    blocks = render_blocks(
        "bulletin", "SVPP is listed as open through 2026-08-04.", "", ""
    )
    assert [b["type"] for b in blocks] == ["header", "section"]
    assert blocks[0]["text"]["text"] == "PROGRAM BULLETIN"


@pytest.mark.parametrize(
    ("kind", "header"),
    [
        ("platinum", "PLATINUM · Verified award"),
        ("nugget", "GOLD · Verified award"),
        ("rfp", "SILVER · Open RFP"),
        ("bulletin", "PROGRAM BULLETIN"),
    ],
)
def test_headers_never_overclaim_their_kind(kind: str, header: str) -> None:
    """Each pick() kind gets its exact label; a bulletin asserts no tier."""
    assert render_blocks(kind, "s", "", "")[0]["text"]["text"] == header


def test_unknown_kind_is_loud() -> None:
    """A kind pick() cannot produce must crash, matching the builder lookup."""
    with pytest.raises(KeyError):
        render_blocks("digest", "s", "", "")


def test_no_evidence_gated_elements_can_render() -> None:
    """No buttons and no rich-only sections exist on this path, whatever the input."""
    blocks = render_blocks(
        "platinum", "sentence", "\n\n<@U1>", "\n\n<https://a.test|x>"
    )
    assert all(b["type"] != "actions" for b in blocks)
    flat = " ".join(_block_texts(blocks))
    for rich_only in ("Contact", "Salesforce", "Spend window", "Persequor"):
        assert rich_only not in flat


# ------------------------------------------------------- run_drip integration
def test_posted_card_carries_blocks_that_mirror_the_text(tmp_path: Path) -> None:
    """End-to-end: blocks reach Slack, and every block is a substring of `text`.

    The substring invariant IS the honesty property of the restyle: blocks that can
    only repeat the proven text cannot introduce a new claim.
    """
    conn = db.connect(tmp_path / "t.db")
    mk_lead(
        conn,
        iid="PA9",
        entity="Bethlehem Area School District",
        start="2025-10-10",
        end="2028-09-30",
    )
    conn.execute("UPDATE leads SET state='PA' WHERE source_item_id='PA9'")
    conn.commit()
    client = SlackClient()
    assert drip.run_drip(client, "C1", conn, force=True).startswith("posted")
    text = str(client.last_kwargs["text"])
    blocks = client.last_kwargs["blocks"]
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "GOLD · Verified award"
    for fragment in _block_texts(blocks)[1:]:  # the header label is layout, not a fact
        assert fragment.strip() in text
    assert any("<@U08C1NBH875>" in t for t in _block_texts(blocks))


# --------------------------------------------------------- fallback_to_daily
def test_fallback_covers_exactly_the_cannot_post_today_outcomes() -> None:
    """True only when nothing posted, no Slack call happened, and rich is done today."""
    cutoff = f"skip: missed the {pacing.HARD_CUTOFF_PT:%H:%M} Pacific hard cutoff"
    for outcome in (
        "skip: no rich award card satisfies every evidence rule",
        "skip: candidate changed after preparation; no post attempted",
        "skip: this stable award/audience delivery already exists",
        "skip: this rich-card delivery is already reserved",
        cutoff,
    ):
        assert delivery.fallback_to_daily(outcome), outcome


def test_fallback_refuses_posted_pending_and_failure_outcomes() -> None:
    """Cap, waiting, weekend, guard, ambiguous, and loud failures never fall through."""
    for outcome in (
        "posted rich_award for lead #7: Bartlett ISD",
        "posted rich_award, but bookkeeping hit RuntimeError; no retry",
        "[dry-run] would post rich_award for lead #7: GOLD: ...",
        "skip: daily cap reached (1)",
        "skip: waiting for today's 10:07 Pacific slot",
        "skip: weekend",
        "blocked: Slack rejected this channel (invalid_auth); no lead consumed",
        "blocked: channel hold active (invalid_auth)",
        "backoff: Slack rate limit active; no lead consumed",
        "backoff: channel hold active (ratelimited)",
        "unknown: Slack delivery could not be confirmed; no retry",
        "error: Slack rejected the rich card; reservation released",
        "quarantined: Slack rejected this rich card (invalid_blocks)",
        "quarantined: rich card could not be rendered; no Slack call attempted",
    ):
        assert not delivery.fallback_to_daily(outcome), outcome


def test_fallback_cutoff_string_tracks_the_pacing_constant() -> None:
    """The classifier and pacing derive the cutoff text from ONE constant, so the
    skip string pacing actually emits is always classified as fallback-eligible."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE posts(id INTEGER,channel TEXT,posted_at TEXT);"
        # `state` mirrors the real schema: the daily-cap count excludes reservations
        # that provably never reached Slack, so a stub without it cannot exercise it.
        "CREATE TABLE notification_outbox(id INTEGER,lead_id INTEGER,"
        "audience TEXT,created_at TEXT,state TEXT);"
        "CREATE TABLE proactive_daily_slots(audience TEXT,local_date TEXT,delivery_kind TEXT,delivery_key TEXT,reserved_at TEXT);"
    )
    from datetime import datetime, timezone

    late = datetime(2026, 7, 22, 18, 30, tzinfo=timezone.utc)  # 11:30 PT
    go, reason = pacing.should_post(conn, "C", late)
    assert go is False
    assert delivery.fallback_to_daily(f"skip: {reason}")
