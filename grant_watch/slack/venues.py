"""Where Grant is allowed to speak, who may speak to it there, and how it reads each.

Split out of `grant.py` on 2026-08-17, when adding the direct-message venue pushed
that file past the 1000-line cap. Everything here answers one question — "is this a
place Grant may answer, and what has been said in it" — and none of it holds app
state, so a change here cannot alter what a confirmation writes.

THE CHANNEL GATE USED TO BE THE ENTIRE AUTHORIZATION STORY. `in_configured_channel`
fails closed on anything that is not an explicitly configured `C…`, and a DM could
never pass it: a DM's `D…` id is minted per person and can never appear in
`SLACK_CHANNEL_ID`. So allowing DMs moves the boundary from the ROOM to the PERSON,
and that boundary has to be BUILT rather than inherited by deleting a condition.

It matters more than it looks. Every member of the workspace can open a DM with an
installed app, an app DM is invisible to everyone else, and one Grant turn can spend
real money: contact enrichment buys Firecrawl scrapes and ZoomInfo credits, and
CLAUDE.md records that a single Slack message can start ~1,000 Firecrawl calls. So a
DM is honored only from the reviewed roster in `config/reps.json` — the same exact
identity boundary already used at every other external-action surface, and the same
fail-closed posture: an unreadable or malformed roster authorizes nobody.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any  # Slack Bolt event payloads are runtime-shaped.

from slack_sdk import WebClient

from .. import roster
from ..config import configured_channel_ids

# One short, deterministic reply per unrecognized sender per process. A stranger who
# deliberately types at Grant and gets NOTHING is the silent-drop failure this repo
# keeps paying for; a canned line costs no model call, no tool, no money and no
# database row. The set bounds it so a loop cannot turn Grant into a noise source.
_TOLD_UNKNOWN: set[str] = set()

UNKNOWN_SENDER_REPLY = (
    "I can only take requests from the Monarch sales roster, so I can't act on this "
    "one. Ask Chase to add you, or find me in the team channel."
)


# --------------------------------------------------------------------- channels
def in_configured_channel(event: Mapping[str, Any]) -> bool:
    """Allow conversations only in Grant's explicitly configured channel(s).

    `SLACK_CHANNEL_ID` may list several channels (e.g. production plus the dev
    playground); a mention in ANY of them is honored, but never a DM."""
    allowed = set(configured_channel_ids())
    item = event.get("item") or {}
    channel = event.get("channel") or item.get("channel")
    return bool(allowed and channel in allowed and event.get("channel_type") != "im")


def active_human_channel_member(client: WebClient, user_id: str, channel: str) -> bool:
    """Recheck active human identity and configured-channel membership at commit."""
    try:
        user = client.users_info(user=user_id).get("user") or {}
        if user.get("deleted") or user.get("is_bot") or user.get("is_app_user"):
            return False
        cursor = ""
        while True:
            response = client.conversations_members(
                channel=channel, limit=200, cursor=cursor or None
            )
            if user_id in response.get("members", []):
                return True
            cursor = str(
                (response.get("response_metadata") or {}).get("next_cursor") or ""
            )
            if not cursor:
                return False
    except Exception:
        return False


# -------------------------------------------------------------- direct messages
def is_dm_channel(channel: object) -> bool:
    """True for a Slack direct-message conversation id."""
    return str(channel or "").startswith("D")


def is_direct_message(event: Mapping[str, Any]) -> bool:
    """True when this event is a DM to Grant from an approved roster member.

    Both the declared `channel_type` AND the channel id are checked: an event that
    claims `im` while naming a `C…` channel is not a DM, and treating it as one would
    hand the DM path a room full of people.
    """
    if str(event.get("channel_type") or "") != "im":
        return False
    if not is_dm_channel(event.get("channel")):
        return False
    return is_approved_sender(event.get("user"))


def is_approved_sender(slack_user_id: object) -> bool:
    """True when this Slack id is an exact reviewed roster identity.

    A broken roster file must authorize nobody rather than crash the listener, so a
    malformed `reps.json` reads as "not approved" — the same answer as an unknown id.
    """
    try:
        return roster.email_for_slack(slack_user_id) is not None
    except Exception:  # noqa: BLE001 — an unreadable roster authorizes nobody.
        return False


def may_converse(event: Mapping[str, Any]) -> bool:
    """The venues Grant answers in: a configured channel, or a roster member's DM.

    Deliberately an OR of two independent gates rather than a loosened channel check.
    `in_configured_channel` still fails closed on every DM, so nothing about the
    channel rule moved; `is_direct_message` carries its own authorization (the
    reviewed roster), because a DM has no room to be trusted for.
    """
    return in_configured_channel(event) or is_direct_message(event)


def decline_unknown_dm(event: Mapping[str, Any], client: WebClient) -> None:
    """Tell someone off the roster who DMs Grant that Grant cannot act — once.

    Silence is the worst available answer here: the person deliberately typed at
    Grant, so nothing arriving reads as BROKEN rather than as declined, and a silent
    drop in the listener is the failure this repo has already paid for twice. The
    reply is a fixed string — no model call, no tool, no spend, no database row — so
    an unapproved sender still cannot make Grant do any work on their behalf.

    Callers pass every rejected event; this returns immediately unless the venue is
    genuinely a DM, so a message in an unconfigured CHANNEL stays as silent as it was.
    """
    if (
        str(event.get("channel_type") or "") != "im"
        or not is_dm_channel(event.get("channel"))
        or is_approved_sender(event.get("user"))
        or event.get("bot_id")
        or not str(event.get("user") or "")
    ):
        return
    if not _should_warn_unknown_sender(event.get("channel"), event.get("user")):
        return
    try:
        client.chat_postMessage(
            channel=str(event["channel"]),
            text=UNKNOWN_SENDER_REPLY,
        )
    except Exception:  # noqa: BLE001 — a courtesy line never breaks the listener.
        return


def _should_warn_unknown_sender(channel: object, slack_user_id: object) -> bool:
    """True the first time an unapproved sender DMs Grant in this process."""
    key = f"{channel}:{slack_user_id}"
    if key in _TOLD_UNKNOWN:
        return False
    _TOLD_UNKNOWN.add(key)
    return True


# ------------------------------------------------------------------- what was said
def thread_history(client: WebClient, channel: str, thread_ts: str | None) -> list[str]:
    """Recent turns as 'Grant: ...' / 'rep: ...' lines, so the offer→confirm flow
    works (Grant remembers it just offered Persequor). Failure -> no context, never
    a crash.

    A top-level DM has no thread to read. `conversations.replies` against it returns
    that ONE message, so every DM would arrive with no memory and a bare "yes" would
    lose its antecedent — the Kerry bug described in `grant._converse_general`,
    reproduced in the one venue where people type consecutive sentences.

    `conversations.history` returns NEWEST first while `conversations.replies`
    returns oldest first. Reading a DM in the raw order would hand the model the
    transcript backwards — still an answer, just to a conversation that never
    happened, and nothing would raise.
    """
    try:
        if thread_ts:
            messages = list(
                client.conversations_replies(
                    channel=channel, ts=thread_ts, limit=12
                ).get("messages", [])
            )
        else:
            messages = list(
                client.conversations_history(channel=channel, limit=12).get(
                    "messages", []
                )
            )
            messages.reverse()
    except Exception:
        return []
    lines: list[str] = []
    for m in messages:
        who = "Grant" if m.get("bot_id") or m.get("app_id") else "rep"
        txt = re.sub(r"<@[^>]+>", "", m.get("text") or "").strip()
        if txt:
            lines.append(f"{who}: {txt}")
    return lines[-10:]
