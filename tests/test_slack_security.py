"""Slack authorization helpers for externally mutating Salesforce confirmations."""

from __future__ import annotations

from grant_watch.slack import grant


class MembershipClient:
    """Deterministic Slack identity and paginated channel-membership boundary."""

    def __init__(self, *, deleted: bool = False, bot: bool = False,
                 fail: bool = False) -> None:
        self.deleted = deleted
        self.bot = bot
        self.fail = fail
        self.member_calls = 0

    def users_info(self, *, user: str) -> dict[str, object]:
        """Return an active/deleted/bot user record."""
        if self.fail:
            raise RuntimeError("Slack unavailable")
        return {"user": {"id": user, "deleted": self.deleted,
                         "is_bot": self.bot, "is_app_user": False}}

    def conversations_members(self, **_kwargs: object) -> dict[str, object]:
        """Put the target on page two to prove cursor traversal."""
        self.member_calls += 1
        if self.member_calls == 1:
            return {"members": ["UOTHER"],
                    "response_metadata": {"next_cursor": "page-two"}}
        return {"members": ["UCHASE"],
                "response_metadata": {"next_cursor": ""}}


def test_active_human_membership_checks_all_pages() -> None:
    """A valid requester is accepted only after active identity and membership proof."""
    client = MembershipClient()
    assert grant._active_human_channel_member(client, "UCHASE", "CGRANTS") is True
    assert client.member_calls == 2


def test_deleted_bot_or_slack_outage_fails_closed() -> None:
    """Unverifiable identities cannot reach the Salesforce confirmation handler."""
    assert grant._active_human_channel_member(
        MembershipClient(deleted=True), "UCHASE", "CGRANTS") is False
    assert grant._active_human_channel_member(
        MembershipClient(bot=True), "UCHASE", "CGRANTS") is False
    assert grant._active_human_channel_member(
        MembershipClient(fail=True), "UCHASE", "CGRANTS") is False


def test_crm_confirmation_blocks_bind_action_and_nonce() -> None:
    """The rendered button carries only the stored action ID and one-time nonce."""
    blocks = grant._crm_action_blocks([{
        "action_id": "action-1", "nonce": "nonce-1",
        "preview": "Create Campaign QA", "expires_at": "2026-07-14T12:15:00+00:00",
    }])
    assert len(blocks) == 3
    serialized = str(blocks)
    assert "salesforce_confirm" in serialized
    assert "action-1" in serialized and "nonce-1" in serialized


def test_crm_preview_splits_without_omitting_frozen_mapping() -> None:
    """Large membership previews stay within Slack limits and preserve every line."""
    preview = "\n".join(f"• District {index} → Lead {index}" for index in range(200))
    blocks = grant._crm_action_blocks([{
        "action_id": "action-1", "nonce": "nonce-1", "preview": preview,
        "expires_at": "2026-07-14T12:15:00+00:00",
    }])
    sections = [block for block in blocks if block["type"] == "section"]
    assert len(sections) > 1
    assert all(len(str(block["text"]["text"])) <= 2_800 for block in sections)
    rendered = "\n".join(str(block["text"]["text"]) for block in sections)
    assert "District 0" in rendered and "District 199" in rendered


def test_interaction_thread_uses_container_then_message_root() -> None:
    """Salesforce approval binds to the actual immutable Slack thread root."""
    assert grant._interaction_thread_ts({
        "container": {"thread_ts": "root.1"},
        "message": {"thread_ts": "other.1", "ts": "button.1"},
    }) == "root.1"
    assert grant._interaction_thread_ts({
        "message": {"thread_ts": "root.2", "ts": "button.2"},
    }) == "root.2"
