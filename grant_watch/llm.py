"""Shared bounded client policy for every Anthropic request.

Slack handlers, cron jobs, and enrichers must not inherit the SDK's potentially long
default wait/retry behavior.  Keeping these options in one tiny module makes a new
model call bounded by construction and gives tests one policy to pin.
"""

from __future__ import annotations

ANTHROPIC_TIMEOUT_SECONDS = 60.0
ANTHROPIC_MAX_RETRIES = 2


def anthropic_client_options() -> dict[str, float | int]:
    """Return a fresh options mapping for one bounded Anthropic client."""
    return {
        "timeout": ANTHROPIC_TIMEOUT_SECONDS,
        "max_retries": ANTHROPIC_MAX_RETRIES,
    }
