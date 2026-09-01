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


def manager_slack_id() -> str | None:
    """The Slack id an unanswered lead escalates to, or None when it is ambiguous.

    Escalation tells a manager that a named colleague has not responded, so getting
    it wrong is a message about the wrong person to the wrong person. Exactly one
    roster row may carry `manager: true`; zero or several return None and the
    escalation simply does not happen. Fail closed, never guess.
    """
    body = json.loads(REPS_PATH.read_text())
    approved = {item.slack_id for item in identities()}
    managers = [
        str(raw.get("slack_id") or "")
        for raw in body.get("reps", [])
        if raw.get("manager") is True and str(raw.get("slack_id") or "") in approved
    ]
    return managers[0] if len(managers) == 1 else None


def timezone_for_slack(slack_id: object) -> str:
    """The rep's own IANA timezone, or "" when it has not been verified.

    Only ever populated from Slack's own user profile. An absent entry means the
    caller falls back to the coast-to-coast window, so a missing zone can never make
    Grant quieter than it already was — and never noisier either, since the fallback
    is the existing behaviour. A zone guessed from a state or an area code would be
    worse than none: it would look authoritative while being wrong for anyone who
    moved.
    """
    wanted = str(slack_id or "").strip()
    body = json.loads(REPS_PATH.read_text())
    approved = {item.slack_id for item in identities()}
    for raw in body.get("reps", []):
        if str(raw.get("slack_id") or "") == wanted and wanted in approved:
            return str(raw.get("timezone") or "")
    return ""


def display_name_for_slack(slack_id: object) -> str:
    """A rep's display NAME for prose, or "" when the roster cannot name them.

    Display only, never identity: callers resolve who somebody IS through
    `email_for_slack`. This exists so a Slack id never reaches a human surface. A raw
    `U…` is meaningless to a reader, and in Slack's link form it notifies a third
    party who is not part of the exchange. An unreadable roster names nobody rather
    than crashing the caller — "" is a fact ("I cannot say who"), which the caller
    renders differently from "nobody".
    """
    wanted = str(slack_id or "").strip()
    if not wanted:
        return ""
    try:
        return next(
            (
                item.name
                for item in identities()
                if item.slack_id == wanted and item.name
            ),
            "",
        )
    except Exception:  # noqa: BLE001 — an unreadable roster names nobody.
        return ""
