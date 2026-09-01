"""The Slack surface for "I'm taking this one" — resolve, refuse, or record.

WHAT THIS IS NOT. It is not Salesforce ownership, and nothing here may suggest it is.
Grant's Salesforce client is create-only; it cannot set an Owner on anything. The rep
whose message prompted this feature had already been told that, correctly, and then
told there was nothing to record either. Only the second half was worth fixing.

EVERY REFUSAL HERE IS LOUD AND SPECIFIC. A claim that quietly matches the wrong
organization removes a real lead from every proactive surface until a human notices it
stopped appearing — which is a silence nobody can debug. So: more than one
organization refuses and lists them, no match refuses and says so, too many rows
refuses, someone else's claim refuses and names them. The only silent outcome is the
successful one, and it echoes back exactly which lead ids it took.

THE VENUE RULE. A claim made in a DM is quoted to nobody. `nudge_sources` already
refuses to carry a DM's contents into a channel, with the reason written out: it
"would report the contents of somebody's private conversation with Grant". A third
party always gets the fact and the date; they get the words only when the words were
said somewhere they could have read them anyway.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import db, lead_claims, roster
from ..presentation import defuse_mentions, display_entity_name
from .venues import is_approved_sender

# More rows than one organization plausibly has. The ambiguity guard already refuses a
# name matching several organizations, so this only fires on a pathological group —
# and a claim that holds forever is not the place to be relaxed about a surprise.
MAX_CLAIM_LEADS = 5
# How many organizations to name back when a rep's word is ambiguous. Listing forty is
# not a question anybody can answer.
MAX_LISTED = 6
# A claim quotes the rep to a colleague later; a paragraph is not a claim.
MAX_CLAIM_TEXT = 400


def _display_name(slack_id: str) -> str:
    """A rep's display name for prose, or a neutral phrase when they are unknown.

    Never returns the raw `U…` id. An id in prose is meaningless to a reader and, in
    Slack's link form, would notify a third party who is not part of the exchange.
    """
    try:
        for identity in roster.identities():
            if identity.slack_id == slack_id and identity.name:
                return identity.name
    except Exception:  # noqa: BLE001 — an unreadable roster names nobody.
        return "someone else"
    return "someone else"


def _date_phrase(stamp: str) -> str:
    """ "on 1 September" from a stored timestamp, or "" when it cannot be read."""
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    return f" on {parsed.day} {parsed:%B}"


def _org_phrase(organization: lead_claims.Organization) -> str:
    """ "Gobles Public Schools (MI)" — how an organization is named back to a human."""
    name = display_entity_name(organization.entity_name)
    return f"{name} ({organization.state})" if organization.state else name


def _lead_phrase(organization: lead_claims.Organization) -> str:
    """The exact lead ids taken, so a wrong claim is visible in the same message."""
    ids = ", ".join(f"#{lead_id}" for lead_id in organization.lead_ids)
    return f"lead {ids}" if len(organization.lead_ids) == 1 else f"leads {ids}"


def _holder_sentence(held: lead_claims.Claim) -> str:
    """Name who holds a lead, quoting them only if they said it where others could see.

    The quote is defused first: `claim_text` is raw Slack wire text and can carry
    `<!here>`, `<!subteam^S…>` or `<@U…>`, every one of which re-fires when the string
    is posted again.
    """
    who = _display_name(held.slack_user)
    when = _date_phrase(held.claimed_at)
    if not held.audience.startswith("C"):
        # Said in a DM or a group DM: the fact is reportable, the words are not.
        return f"{who} has that one{when}."
    quoted = defuse_mentions(held.claim_text)
    return f'{who} has that one{when} — "{quoted}".'


def _resolve_or_explain(
    conn: sqlite3.Connection, name: str, state: str
) -> tuple[lead_claims.Organization | None, str]:
    """Exactly one organization, or the sentence explaining why Grant will not guess."""
    wanted = name.strip()
    if len(wanted) < 3:
        return None, (
            "ERROR: tell me which organization — a name, not a word like that, "
            "because I hold this until somebody hands it back."
        )
    found = lead_claims.resolve(conn, wanted, state)
    if not found:
        where = f" in {state.strip().upper()}" if state.strip() else ""
        return None, (
            f'I have no lead on file for "{wanted}"{where}, so there is nothing for '
            "me to mark. Tell me the state and I can search properly."
        )
    if len(found) > 1:
        listed = "; ".join(_org_phrase(org) for org in found[:MAX_LISTED])
        more = (
            f" — and {len(found) - MAX_LISTED} more" if len(found) > MAX_LISTED else ""
        )
        return None, (
            f'"{wanted}" matches more than one organization I hold: {listed}{more}. '
            "Which one, or which state?"
        )
    organization = found[0]
    if len(organization.lead_ids) > MAX_CLAIM_LEADS:
        return None, (
            f'"{wanted}" resolves to {len(organization.lead_ids)} separate lead rows, '
            "which is more than I will take on one word. Narrow it and I'll record it."
        )
    return organization, ""


def claim_lead(
    args: dict[str, Any],
    requester_slack: str,
    channel: str,
    thread_ts: str,
    user_text: str,
) -> str:
    """Record (or release) a rep's claim on one organization's leads.

    `user_text` is the rep's OWN message, passed through by the dispatcher rather than
    supplied by the model. That is deliberate: the stored words are quoted back to a
    colleague weeks later, and a model-authored summary of what somebody said is
    exactly the kind of drift rule 1 forbids.
    """
    if not requester_slack:
        return "ERROR: I can't record that without knowing who's asking."
    if not is_approved_sender(requester_slack):
        # Fail closed. Grant later attributes this claim BY NAME to a third party;
        # a claimant it cannot name is a claim it cannot honestly report.
        return (
            "ERROR: I can only track leads for people on the Monarch sales roster, "
            "so I can't record that one."
        )
    conn = db.connect()
    try:
        organization, explanation = _resolve_or_explain(
            conn, str(args.get("name", "")), str(args.get("state", ""))
        )
        if organization is None:
            return explanation
        if bool(args.get("release", False)):
            return _release(conn, organization, requester_slack)
        return _claim(
            conn, organization, requester_slack, channel, thread_ts, user_text
        )
    finally:
        conn.close()


def _claim(
    conn: sqlite3.Connection,
    organization: lead_claims.Organization,
    requester_slack: str,
    channel: str,
    thread_ts: str,
    user_text: str,
) -> str:
    """Take the organization, or explain honestly why it could not be taken."""
    words = defuse_mentions(user_text).strip()[:MAX_CLAIM_TEXT]
    if not words:
        return "ERROR: I need your own words on file before I can record a claim."
    try:
        fresh, mine = lead_claims.claim(
            conn,
            organization,
            slack_user=requester_slack,
            audience=channel,
            thread_ts=thread_ts,
            message_ts=thread_ts,
            claim_text=words,
            now=datetime.now(timezone.utc),
        )
    except lead_claims.AlreadyClaimed as exc:
        return (
            f"{_holder_sentence(exc.held_by)} I've left it with them — if it should "
            "move, either of you can tell me to hand it back and then take it."
        )
    except ValueError as exc:
        return f"ERROR: {exc}."
    if not fresh and mine:
        return (
            f"You already had {_org_phrase(organization)} "
            f"({_lead_phrase(organization)}) — nothing to change."
        )
    return (
        f"Recorded — {_org_phrase(organization)} is yours "
        f"({_lead_phrase(organization)}). I'll keep it out of the daily cards and "
        "won't follow up about it with you or anyone else until somebody hands it "
        "back.\n\nThat's my own record, not Salesforce — I still can't set an owner "
        "there."
    )


def _release(
    conn: sqlite3.Connection,
    organization: lead_claims.Organization,
    requester_slack: str,
) -> str:
    """Hand an organization back, whoever was holding it."""
    held = lead_claims.live_claims(conn, organization.lead_ids)
    if not held:
        return (
            f"Nobody has {_org_phrase(organization)} on my side, so there's nothing "
            "to hand back."
        )
    ended = lead_claims.release(
        conn, organization, released_by=requester_slack, now=datetime.now(timezone.utc)
    )
    return (
        f"Done — {_org_phrase(organization)} is back in the pool ({ended} "
        f"{'row' if ended == 1 else 'rows'}). I can surface it again from here."
    )
