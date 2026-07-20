"""Runtime channel configuration for Grant.

`SLACK_CHANNEL_ID` may name ONE channel or a comma-separated LIST of channels
(e.g. the production channel plus the dev playground). Order matters: the FIRST
id is the PRIMARY channel — proactive drip posts and the orphan-spinner sweep
target it — while EVERY listed id is a valid channel for answering mentions and
for human-approved Salesforce writes.

Kept dependency-free (only `os`) so both the Slack layer and the enrich policy
layer can import it without creating an import cycle.
"""

from __future__ import annotations

import os


def configured_channel_ids() -> list[str]:
    """Return every channel id Grant operates in, in configured order.

    Parses `SLACK_CHANNEL_ID` as a comma-separated list; surrounding whitespace
    is trimmed and blank entries dropped. Returns an empty list when the variable
    is unset or blank (callers treat that as "no channel configured")."""
    raw = os.environ.get("SLACK_CHANNEL_ID", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def primary_channel_id() -> str:
    """Return the PRIMARY channel — the first configured id — used for proactive
    drip posts and the boot-time spinner sweep. Empty string when none is set."""
    ids = configured_channel_ids()
    return ids[0] if ids else ""
