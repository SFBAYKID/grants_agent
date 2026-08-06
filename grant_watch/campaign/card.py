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

from ..presentation import display_entity_name, state_display_name
from .policy import is_website_ownership_proven
from .routing import RoutingReason
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


def _short_date(value: str) -> str:
    """Render one ISO date compactly for the spend window ("Oct 1, 2025").

    The award date deliberately keeps the long form — it is the load-bearing claim —
    while the window is a range and reads better short (Chase's approved layout,
    2026-08-06). An unparseable value degrades to the stored text, never to a guess.
    """
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return safe_text(value, 30)
    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


def _award_date(value: str, precision: str) -> str:
    """Render only the date precision supported by the frozen evidence."""
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return safe_text(value, 30)
    if precision == "month":
        return parsed.strftime("%B %Y")
    return _date(value)


def _research_note(draft: SnapshotDraft) -> str:
    """The honest reason(s) an eligible card still needs human review before drafting:
    an ambiguous Salesforce match and/or a website whose org ownership is only inferred
    from a name match (not an exact authoritative record)."""
    reasons: list[str] = []
    if draft.sf_lookup_status == "ambiguous":
        reasons.append("Salesforce match is ambiguous")
    if not is_website_ownership_proven(draft.official_website_provenance):
        # Shortened 2026-08-06 at Chase's request. Still states the limitation plainly:
        # the website was NOT matched against an exact authoritative record. It must
        # never soften into implying the website is confirmed.
        reasons.append("Website not exact-matched")
    return "; ".join(reasons) or "Details unconfirmed"


def _event_date_label(event_type: str) -> str:
    """Label the evidenced event date in the source's exact terms.

    'Federal funds obligated' for an obligation; 'Award announced' for an announcement.
    An obligation is never collapsed into a bare 'Awarded' (which would overclaim)."""
    return (
        "Federal funds obligated"
        if event_type == "award_obligated"
        else "Award announced"
    )


def fallback_text(draft: SnapshotDraft) -> str:
    """Create complete screen-reader/notification text from deterministic facts."""
    tier = draft.tier.upper()
    # No owner → no routing sentence at all (Chase 2026-08-05: never say
    # "unassigned territory" on a card; just don't tag anyone).
    route = (
        f"Assigned to <@{draft.route.slack_user_id}>. "
        if draft.route.slack_user_id
        else ""
    )
    # The comma belongs to the TITLE, not the name, so it is emitted only when a title
    # exists. contact_evidence often verifies a named person without one, and the
    # unconditional form rendered "Contact: Dalton Cagle, — dalton@…" — a visibly broken
    # line on the phone lock screen, which is the only place this text is read
    # (the Block Kit field already guards the title separately). Chase 2026-08-06.
    named = safe_text(draft.contact_name, 120)
    title = safe_text(draft.contact_title, 120)
    contact = (
        f"Contact: {f'{named}, {title}' if title else named} — "
        f"{safe_text(draft.contact_email, 254)}."
        if draft.contact_name
        else f"Official general mailbox: {safe_text(draft.contact_email, 254)}."
    )
    # The display text already ends in a period; strip any trailing dots so the template
    # adds exactly one (never "…net-new..").
    crm_text = safe_text(draft.sf_display_text, 500).rstrip(".")
    crm = f" Salesforce: {crm_text}." if crm_text else ""
    # The "Not relevant" button was removed 2026-08-06, so this no longer advertises it.
    # A research card offers no action at all; only a draft-ready card does.
    actions = (
        f"{_research_note(draft)} — confirm before outreach."
        if draft.card_mode == "research_needed"
        else "Actions: Ask Persequor to draft."
    )
    # Humanized to match the card face, so the lock-screen text and the rendered blocks
    # name the same organization the same way.
    entity = safe_text(display_entity_name(draft.entity_name, 180), 180)
    where = state_display_name(draft.state) or safe_text(draft.state, 2)
    text = (
        f"{tier}: {entity} in {where} "
        f"has a verified {_money(draft.amount)} {safe_text(draft.program, 120)} "
        f"funding award. {_event_date_label(draft.event_type)}: "
        f"{_award_date(draft.award_date, draft.award_date_precision)}. "
        f"Spend window: {_date(draft.spend_window_start)} "
        f"through {_date(draft.spend_window_end)}. {route}{crm} {contact} "
        f"{actions}"
    )
    return " ".join(text.split())[:MAX_FALLBACK]


def _link(url: str, label: str) -> str:
    """Render one accurately labelled Slack link when its URL is safe."""
    safe = safe_url(url)
    return f"<{safe}|{label}>" if safe else ""


def render(snapshot: FrozenSnapshot) -> RenderedCard:
    """Render the frozen snapshot as controlled Block Kit plus accessible text."""
    draft = snapshot.draft
    research = draft.card_mode == "research_needed"
    # Name the routing reason honestly: a Salesforce owner is a relationship; a
    # territory owner is not. A research-needed card only ever routes by territory.
    # No owner → NO routing block at all (Chase 2026-08-05: a card without a mapped
    # rep simply tags nobody; it no longer says "unassigned territory").
    route_text = ""
    if draft.route.slack_user_id:
        owner_kind = (
            "territory owner"
            if draft.route.reason is RoutingReason.TERRITORY
            else "relationship owner"
        )
        route_text = f"<@{draft.route.slack_user_id}> — {owner_kind}"
    # Chase 2026-08-06: the first live card read as a wall of shouting, because the rich
    # path printed the raw USAspending recipient name ("HOXIE SCHOOL DISTRICT NO 46")
    # while the legacy card had always humanized it. Both helpers are the drip card's,
    # and both fail safe: display_entity_name strips <>*_~|@` and state_display_name
    # returns "" for an unknown code rather than printing a bare two-letter code.
    # safe_text AFTER display_entity_name, deliberately: the humanizer strips <>*_~|@`
    # (so a hostile source name cannot inject a mention or link at all) but does NOT
    # escape `&`, which is Slack's own escape character. Composing them gives both.
    named = safe_text(display_entity_name(draft.entity_name, 180), 180)
    located = state_display_name(draft.state)
    award = (
        f"*{named}*" + (f" — {located}" if located else "") + "\n"
        f"{_money(draft.amount)} · {safe_text(draft.program, 120)}\n"
        f"{_event_date_label(draft.event_type)} "
        f"{_award_date(draft.award_date, draft.award_date_precision)}"
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
    ]
    if route_text:
        blocks.append(
            {
                "type": "section",
                # The only markup intentionally preserved is a roster-validated Slack
                # id frozen by routing. No source-controlled text enters this line.
                "text": {"type": "mrkdwn", "text": route_text},
            }
        )
    blocks.extend(
        [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": award[:MAX_SECTION]},
            },
            {"type": "divider"},
            {
                "type": "section",
                # Stacked, NOT a two-field section. Slack lays fields out side by side,
                # which is what made the spend window and contact collide into one dense
                # run of text on the first live card.
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Spend window*\n{_short_date(draft.spend_window_start)} – "
                        f"{_short_date(draft.spend_window_end)}\n\n"
                        f"*Contact*\n{safe_text(draft.contact_name or 'Official general mailbox', 120)}"
                        + (
                            f" · {safe_text(draft.contact_title, 120)}"
                            if draft.contact_title
                            else ""
                        )
                        + f"\n{safe_text(draft.contact_email, 254)}"
                    )[:MAX_SECTION],
                },
            },
        ]
    )
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
        _link(draft.award_url, "Award record"),
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
    if research:
        # No active draft action until a human confirms the flagged evidence.
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_{_research_note(draft)} — confirm before outreach._",
                    }
                ],
            }
        )
    # Chase 2026-08-06 removed the "Not relevant" button: the card is information, not a
    # control surface. (It had also never worked — SLACK_WORKSPACE_ID was absent from
    # production, so `actions._authorized_snapshot` refused every click on its first
    # gate.) The Persequor draft button survives for a draft-ready card, but no lead can
    # currently reach that mode because `leads.nces_website` has no writer, so in
    # practice today every card renders with NO actions block at all. An `actions` block
    # with an empty `elements` list is invalid Block Kit and Slack rejects the whole
    # message, so the block is omitted rather than emitted empty.
    action_elements: list[dict[str, Any]] = []
    if not research:
        action_elements.append(
            {
                "type": "button",
                "action_id": "rich_persequor_draft",
                "text": {"type": "plain_text", "text": "Ask Persequor to draft"},
                "value": snapshot.id,
            }
        )
    if action_elements:
        blocks.append({"type": "actions", "elements": action_elements})
    text = draft.fallback_text or fallback_text(draft)
    return RenderedCard(text=text[:MAX_FALLBACK], blocks=tuple(blocks))
