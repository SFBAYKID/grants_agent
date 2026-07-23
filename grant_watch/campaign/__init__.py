"""Rich proactive award-card campaign (feature-flagged OFF).

A nationwide, precision-first campaign that surfaces at most one highly-qualified
city-or-school AWARD card per eligible weekday, binding verified award evidence, fresh
public contact, fresh Salesforce context, honest rep routing, and safe actions into an
immutable snapshot. Design: `docs/rich_award_card_design.md`.

NOTHING here is enabled by default. `GRANT_RICH_CARD_ENABLED` gates delivery and defaults
OFF; `--dry-run` writes nothing; shadow mode prepares local state only. See `policy.py`
for the eligibility predicate and the single source of the feature flag.
"""

from __future__ import annotations

import os


def rich_card_enabled() -> bool:
    """Whether the rich award-card campaign is enabled. Defaults OFF.

    The ONLY reader of the flag. Enabling requires an explicit truthy env value AND is
    still gated by every per-card eligibility rule and by human approval on any action.
    """
    return os.environ.get("GRANT_RICH_CARD_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
