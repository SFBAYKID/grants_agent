"""Multi-channel configuration: SLACK_CHANNEL_ID may list several channels.

The first id is the PRIMARY (drip posts + spinner sweep); every listed id may
answer mentions and host human-approved Salesforce writes. Backward compatibility
with a single-channel value is asserted alongside the new list behavior."""

from __future__ import annotations

import pytest

from grant_watch.config import configured_channel_ids, primary_channel_id
from grant_watch.enrich.salesforce_campaign_policy import write_channel_allowed
from grant_watch.slack.grant import _in_configured_channel

PROD = "C01DGT9D11D"  # production channel (monarch-cloud-team-vekada)
PLAY = "C0B02721MNK"  # dev playground


def _mention(channel: str, channel_type: str = "channel") -> dict[str, object]:
    """Build a minimal app_mention-shaped event for the channel gate."""
    return {"channel": channel, "channel_type": channel_type}


def test_configured_channel_ids_single(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single id parses to a one-element list (backward compatible)."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", PROD)
    assert configured_channel_ids() == [PROD]
    assert primary_channel_id() == PROD


def test_configured_channel_ids_list_order_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comma list keeps order, trims whitespace, and drops blank entries."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", f" {PROD} , {PLAY} ,")
    assert configured_channel_ids() == [PROD, PLAY]
    assert primary_channel_id() == PROD  # first = primary (drip target)


def test_configured_channel_ids_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset/blank yields no channels and an empty primary."""
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    assert configured_channel_ids() == []
    assert primary_channel_id() == ""
    monkeypatch.setenv("SLACK_CHANNEL_ID", "   ")
    assert configured_channel_ids() == []


def test_mention_honored_in_every_listed_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both the production and playground channels answer when both are listed."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", f"{PROD},{PLAY}")
    assert _in_configured_channel(_mention(PROD)) is True
    assert _in_configured_channel(_mention(PLAY)) is True


def test_mention_rejected_outside_listed_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlisted channel and any DM are never honored."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", f"{PROD},{PLAY}")
    assert _in_configured_channel(_mention("CZZUNLISTED")) is False
    assert _in_configured_channel(_mention(PROD, channel_type="im")) is False


def test_mention_gate_unconfigured_denies_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no channel configured, nothing is honored (fail closed)."""
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    assert _in_configured_channel(_mention(PROD)) is False


def test_write_allowed_falls_back_to_all_configured_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit write allowlist, writes are allowed in EVERY configured
    channel — not a single raw comma-joined string (the pre-fix bug)."""
    monkeypatch.delenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", raising=False)
    monkeypatch.setenv("SLACK_CHANNEL_ID", f"{PROD},{PLAY}")
    assert write_channel_allowed(PROD) is True
    assert write_channel_allowed(PLAY) is True
    assert write_channel_allowed("CZZUNLISTED") is False
    # The literal joined string must never be treated as one channel id.
    assert write_channel_allowed(f"{PROD},{PLAY}") is False


def test_explicit_write_allowlist_overrides_channel_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit GRANT_SALESFORCE_WRITE_CHANNEL_IDS wins over the channel fallback."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", f"{PROD},{PLAY}")
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", PROD)
    assert write_channel_allowed(PROD) is True
    assert write_channel_allowed(PLAY) is False  # not on the explicit allowlist
