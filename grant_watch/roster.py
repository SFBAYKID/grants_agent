"""Exact Monarch roster identity mapping used at external-action boundaries.

Names are display text and never identify a rep. Salesforce ownership and Slack
requesters resolve only through the reviewed email/Slack-id pairs in
``config/reps.json``. Unknown or malformed entries fail closed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPS_PATH = Path(__file__).resolve().parent.parent / "config" / "reps.json"
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,20}$")


@dataclass(frozen=True)
class RosterIdentity:
    """One exact approved roster identity."""

    email: str
    slack_id: str
    name: str


def identities() -> tuple[RosterIdentity, ...]:
    """Load valid exact roster identities without accepting partial records."""
    body = json.loads(REPS_PATH.read_text())
    found: list[RosterIdentity] = []
    for raw in body.get("reps", []):
        email = str(raw.get("email") or "").strip().lower()
        slack_id = str(raw.get("slack_id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if "@" not in email or not _SLACK_ID_RE.fullmatch(slack_id):
            continue
        found.append(RosterIdentity(email=email, slack_id=slack_id, name=name))
    return tuple(found)


def slack_for_email(email: object) -> str | None:
    """Resolve an exact Salesforce owner email to an approved Slack id."""
    wanted = str(email or "").strip().lower()
    return next((item.slack_id for item in identities() if item.email == wanted), None)


def email_for_slack(slack_id: object) -> str | None:
    """Resolve an exact Slack id to its approved roster email."""
    wanted = str(slack_id or "").strip()
    return next((item.email for item in identities() if item.slack_id == wanted), None)
