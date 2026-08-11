"""Did anybody actually answer? Asked of Slack, and allowed to say "I don't know".

THIS IS THE GUARD ON THE MOST DANGEROUS SENTENCE GRANT SAYS. An escalation tells a
manager, in a channel the whole team reads, that a named colleague did not respond.
Every other follow-up is addressed to the person who could correct it; this one is
about somebody, to somebody else. If it is wrong, the person it is about finds out
when they read it.

WHY NOT `slack_event_receipts`. The existing engagement signal reads that table, and
its own docstring says it UNDERCOUNTS: a receipt exists only for events Grant woke for
and processed. Undercounting is the safe direction for an A/B reply rate — it can only
make a wording look worse than it is. It is the CATASTROPHIC direction here, because
"no receipt" would be read as "she ignored you" for a reply Grant simply never saw, and
there are several ordinary ways to be invisible: a missing `message.groups` scope on a
private channel, a listener that was restarted, an event dropped during an outage.

So silence is established against Slack itself, and the check is allowed a third
answer. `None` means "could not be determined", and every caller treats it exactly as
it treats "they replied" — no escalation. Grant does not report silence it cannot see.
"""

from __future__ import annotations

from slack_sdk import WebClient


def replied_since(
    client: WebClient | None,
    channel: str,
    thread_ts: str,
    after_ts: str,
    *,
    exclude_user: str = "",
) -> bool | None:
    """Whether a HUMAN posted in this thread after `after_ts`.

    Returns True (somebody answered), False (verified silence), or None (Grant could
    not tell — treat as answered, never escalate on an unread thread).

    `exclude_user` drops one Slack id from consideration; it exists so an escalation
    about a lead can ignore the manager's own passing comment when deciding whether
    the REP answered. Left empty, any human reply counts.
    """
    if client is None or not channel or not thread_ts or not after_ts:
        return None
    try:
        floor = float(after_ts)
    except (TypeError, ValueError):
        return None
    try:
        response = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=200
        )
    except Exception:  # noqa: BLE001 — an unreadable thread is "unknown", not "silent"
        return None
    messages = response.get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        if not _is_human(message):
            continue
        user = str(message.get("user") or "")
        if exclude_user and user == exclude_user:
            continue
        try:
            when = float(str(message.get("ts") or ""))
        except (TypeError, ValueError):
            continue
        if when > floor:
            return True
    return False


def _is_human(message: dict[str, object]) -> bool:
    """Whether this message was typed by a person rather than posted by software.

    THREE SIGNALS, because one is not enough and each has burned something already in
    this codebase. `bot_id` is absent on messages from Slack APPS posting as a user
    token; `app_id` is what marks the Claude app's own messages, which is why a plain
    reply sent from here does not wake Grant; `subtype` catches `bot_message` and the
    join/leave noise that would otherwise read as a reply.

    An EMPTY `bot_id` must not count as a bot — the watchdog shipped that bug, where a
    falsy id matched every message in the channel. Hence the truthiness checks rather
    than key presence.
    """
    if message.get("bot_id") or message.get("app_id"):
        return False
    if str(message.get("subtype") or ""):
        return False
    return bool(str(message.get("user") or ""))
