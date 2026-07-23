"""Deterministic, accessible Block Kit rendering for frozen rich award cards.

The layout does not depend on external unfurls. Every source-controlled text field is
flattened, escaped, and bounded; links must be public HTTPS URLs without credentials or
sensitive query keys. Action values contain only the opaque snapshot id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from .snapshot import FrozenSnapshot, SnapshotDraft

MAX_FALLBACK = 4000
MAX_SECTION = 3000
MAX_FIELD = 2000
MAX_CONTEXT = 3000
_SENSITIVE_QUERY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?key|auth|credential|password|secret|signature|token)(?:$|[_-])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RenderedCard:
    """Slack payload fragments; transport settings are applied by delivery."""

    text: str
    blocks: tuple[dict[str, Any], ...]


def safe_text(value: object, limit: int) -> str:
    """Flatten, bound, and Slack-escape one untrusted text field."""
    text = " ".join(str(value or "").split())[:limit]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def safe_url(value: object) -> str:
    """Return one bounded public HTTPS URL or an empty fail-closed marker."""
    raw = str(value or "").strip()
    if len(raw) > 2000:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    sensitive = any(
        _SENSITIVE_QUERY_RE.search(key) for key, _ in parse_qsl(parsed.query)
    )
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or sensitive
        or any(char in raw for char in ("<", ">", "|", "\n", "\r"))
    ):
        return ""
    return raw


def exact_award_url(source: str, url: str, locator: str) -> bool:
    """Recognize an exact source record, not a homepage/dataset/parent award."""
    return bool(award_record_identity(source, url, locator))


def award_record_identity(source: str, url: str, locator: str) -> str:
    """Return the source's stable exact-record identity, or empty when unsupported."""
    safe = safe_url(url)
    locator_value = locator.strip()
    if not safe or not locator_value or not source.startswith("usaspending:"):
        return ""
    parsed = urlsplit(safe)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = unquote(parsed.path).rstrip("/")
    if host != "usaspending.gov" or not path.startswith("/award/"):
        return ""
    record_id = path.removeprefix("/award/").split("/", 1)[0].strip()
    return record_id if record_id else ""


def official_site_evidenced(website: str, evidence_url: str) -> bool:
    """Require the persisted website and its evidence page to share an official site."""
    site = urlsplit(safe_url(website)).hostname or ""
    evidence = urlsplit(safe_url(evidence_url)).hostname or ""
    left = site.lower().removeprefix("www.")
    right = evidence.lower().removeprefix("www.")
    return bool(
        left
        and right
        and (left == right or left.endswith(f".{right}") or right.endswith(f".{left}"))
    )


def _money(amount: float) -> str:
    """Render an evidenced award amount without implying a remaining balance."""
    return f"${amount:,.0f}"


def _date(value: str) -> str:
    """Render one ISO date for people, preserving the exact stored day."""
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return safe_text(value, 30)
    return parsed.strftime("%B %d, %Y").replace(" 0", " ")


def _award_date(value: str, precision: str) -> str:
    """Render only the date precision supported by the frozen evidence."""
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return safe_text(value, 30)
    if precision == "month":
        return parsed.strftime("%B %Y")
    return _date(value)


def _award_when(value: str, precision: str) -> str:
    """Return a grammatical, precision-safe award-date phrase."""
    preposition = "in" if precision == "month" else "on"
    return f"{preposition} {_award_date(value, precision)}"


def fallback_text(draft: SnapshotDraft) -> str:
    """Create complete screen-reader/notification text from deterministic facts."""
    tier = draft.tier.upper()
    route = (
        f"Assigned to <@{draft.route.slack_user_id}>."
        if draft.route.slack_user_id
        else "Unassigned territory."
    )
    contact = (
        f"Contact: {safe_text(draft.contact_name, 120)}, "
        f"{safe_text(draft.contact_title, 120)} — {safe_text(draft.contact_email, 254)}."
        if draft.contact_name
        else f"Official general mailbox: {safe_text(draft.contact_email, 254)}."
    )
    crm = (
        f" Salesforce: {safe_text(draft.sf_display_text, 500)}."
        if draft.sf_display_text
        else ""
    )
    text = (
        f"{tier}: {safe_text(draft.entity_name, 180)} in {safe_text(draft.state, 2)} "
        f"received a verified {_money(draft.amount)} {safe_text(draft.program, 120)} "
        f"funding award {_award_when(draft.award_date, draft.award_date_precision)}. "
        f"Spend window: {_date(draft.spend_window_start)} "
        f"through {_date(draft.spend_window_end)}. {route}{crm} {contact} "
        "Actions: Ask Persequor to draft; Not relevant."
    )
    return " ".join(text.split())[:MAX_FALLBACK]


def _link(url: str, label: str) -> str:
    """Render one accurately labelled Slack link when its URL is safe."""
    safe = safe_url(url)
    return f"<{safe}|{label}>" if safe else ""


def render(snapshot: FrozenSnapshot) -> RenderedCard:
    """Render the frozen snapshot as controlled Block Kit plus accessible text."""
    draft = snapshot.draft
    route_text = (
        f"<@{draft.route.slack_user_id}> — relationship/territory owner"
        if draft.route.slack_user_id
        else "_Unassigned territory — no verified owner mapped_"
    )
    award = (
        f"*{safe_text(draft.entity_name, 180)}* · {safe_text(draft.state, 2)}\n"
        f"Verified {_money(draft.amount)} {safe_text(draft.program, 120)} funding award\n"
        f"*Award date:* {_award_date(draft.award_date, draft.award_date_precision)}"
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{draft.tier.upper()} · Verified award",
                "emoji": True,
            },
        },
        {
            "type": "section",
            # The only markup intentionally preserved is a roster-validated Slack id
            # frozen by routing. No source-controlled text enters this line.
            "text": {"type": "mrkdwn", "text": route_text},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": award[:MAX_SECTION]},
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Spend window*\n{_date(draft.spend_window_start)} – "
                        f"{_date(draft.spend_window_end)}"
                    )[:MAX_FIELD],
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Contact*\n{safe_text(draft.contact_name or 'Official general mailbox', 120)}"
                        + (
                            f" · {safe_text(draft.contact_title, 120)}"
                            if draft.contact_title
                            else ""
                        )
                        + f"\n{safe_text(draft.contact_email, 254)}"
                    )[:MAX_FIELD],
                },
            ],
        },
    ]
    if draft.sf_display_text:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ("*Salesforce*\n" + safe_text(draft.sf_display_text, 500))[
                        :MAX_SECTION
                    ],
                },
            }
        )
    links = [
        _link(draft.official_website, "Official website"),
        _link(draft.contact_evidence_url, "Contact evidence"),
        _link(draft.sf_open_link, "Open Salesforce") if draft.sf_open_link else "",
        _link(draft.award_url, "View exact award record"),
    ]
    accepted: list[str] = []
    for link in filter(None, links):
        candidate = " · ".join((*accepted, link))
        if len(candidate) <= MAX_CONTEXT:
            accepted.append(link)
    valid_links = " · ".join(accepted)
    if valid_links:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": valid_links}],
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "rich_persequor_draft",
                    "text": {"type": "plain_text", "text": "Ask Persequor to draft"},
                    "value": snapshot.id,
                },
                {
                    "type": "button",
                    "action_id": "rich_not_relevant",
                    "text": {"type": "plain_text", "text": "Not relevant"},
                    "style": "danger",
                    "value": snapshot.id,
                },
            ],
        }
    )
    text = draft.fallback_text or fallback_text(draft)
    return RenderedCard(text=text[:MAX_FALLBACK], blocks=tuple(blocks))
