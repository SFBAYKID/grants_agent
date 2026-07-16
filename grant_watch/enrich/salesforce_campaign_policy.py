"""Fail-closed policy checks for Salesforce Campaign actions."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .. import db
from .salesforce_campaign_gateway import SalesforceRecordRef


def now_utc() -> datetime:
    """Return an aware UTC clock value for approvals and audit state."""
    return datetime.now(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    """Serialize an aware timestamp consistently."""
    return value.isoformat(timespec="seconds")


def write_channel_allowed(channel: str) -> bool:
    """Allow writes only in configured Grant channels, never arbitrary DMs/channels."""
    configured = os.environ.get("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "")
    values = {item.strip() for item in configured.split(",") if item.strip()}
    if not values:
        fallback = os.environ.get("SLACK_CHANNEL_ID", "").strip()
        values = {fallback} if fallback else set()
    return bool(channel and channel in values)


def writer_enabled() -> bool:
    """Return whether external Campaign writes are explicitly feature-enabled."""
    return os.environ.get("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0") == "1"


def validate_action_context(
    workspace: str, channel: str, thread_ts: str, requester: str
) -> None:
    """Validate the immutable Slack action context before storing a preview."""
    if not all((workspace, channel, thread_ts, requester)):
        raise ValueError(
            "Salesforce actions require workspace, channel, thread, and user"
        )
    if not write_channel_allowed(channel):
        raise PermissionError(
            "Salesforce writes are limited to configured Grant channels"
        )


def record_matches_organization(
    record: SalesforceRecordRef, entity_name: str, state: str
) -> bool:
    """Require a supplied/found person record to belong to the Grant organization."""
    if not record.company.strip():
        return False
    expected_name = db.canonical_entity_key(entity_name).partition("|")[0]
    record_name = db.canonical_entity_key(record.company).partition("|")[0]
    if expected_name != record_name:
        return False
    return not (state and record.state and state.upper() != record.state.upper())
