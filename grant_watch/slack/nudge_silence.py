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
"no receipt" would be read as "she ignored you" for a reply Grant simply never saw.

THE FIRST VERSION OF THIS MODULE INHERITED THE LISTENER'S BLIND SPOT INSTEAD OF
CORRECTING FOR IT, which defeated the point of asking Slack at all. An adversarial
review reproduced four separate ways to post a false accusation, three of them on
completely ordinary replies. All four are fixed here and each is pinned by a test:

  * a reply carrying ANY `subtype` was classed as software. `file_share` is what Slack
    puts on "here's the list you asked for" — the single most likely shape of the very
    reply being chased — and `thread_broadcast` is the "also send to channel" checkbox.
    Now an explicit DENY list of subtypes that are genuinely not a person;
  * a thread longer than one page reported VERIFIED SILENCE, because `has_more` was
    ignored and Slack returns replies OLDEST FIRST, so the truncated tail is exactly
    the recent part that matters. Now paged, and an exhausted budget returns None;
  * a REACTION on the message was invisible, though this codebase elsewhere calls a
    reaction "the cheapest +1 there is". The payload already carried it;
  * "did anyone speak" was used to answer "did THIS PERSON answer", so a passing
    comment from an uninvolved colleague permanently retired the follow-up.

The check is allowed a third answer. `None` means "could not be determined", and every
caller treats it exactly as it treats "they replied" — no escalation. Grant does not
report silence it cannot see.
"""

from __future__ import annotations

from slack_sdk import WebClient

# Message subtypes that are genuinely NOT a person talking. Everything not listed here
# counts as human, which is the correct default: Slack adds subtypes freely, and the
# cost of a new one is asymmetric — treating a person's reply as software produces a
# public accusation, while treating software as a person merely produces silence.
NON_HUMAN_SUBTYPES = frozenset(
    {
        "bot_message",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "message_changed",
        "message_deleted",
        "tombstone",
        "reminder_add",
    }
)

# How many pages of a thread to read before giving up and saying "unknown". Slack
# returns up to 200 replies a page oldest-first, so this covers a 2,000-message thread;
# beyond that the honest answer is that Grant did not read it all.
MAX_PAGES = 10
PAGE_SIZE = 200


def replied_since(
    client: WebClient | None,
    channel: str,
    thread_ts: str,
    after_ts: str,
    *,
    only_user: str = "",
    exclude_user: str = "",
) -> bool | None:
    """Whether a HUMAN answered in this thread after `after_ts`.

    Returns True (somebody answered), False (verified silence), or None (Grant could
    not tell — treat as answered, never escalate on a thread it could not read).

    `only_user` narrows the question to ONE person: "did Jocelyn answer", which is what
    an unanswered-offer escalation actually claims. Without it a passing remark from an
    uninvolved colleague counted as her reply and permanently retired the follow-up.
    `exclude_user` drops one id instead, so an escalation about a card can ignore the
    manager's own comment. They are mutually exclusive in practice; `only_user` wins.

    A REACTION COUNTS. Reactions carry no timestamp, so one cannot be placed relative
    to `after_ts` — and that is fine, because the direction of the error is what
    matters: counting an older reaction suppresses a message, and suppressing is always
    the safe failure here.
    """
    if client is None or not channel or not thread_ts or not after_ts:
        return None
    try:
        floor = float(after_ts)
    except (TypeError, ValueError):
        return None

    cursor = ""
    for _ in range(MAX_PAGES):
        try:
            response = client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=PAGE_SIZE,
                **({"cursor": cursor} if cursor else {}),
            )
        except Exception:  # noqa: BLE001 — unreadable is "unknown", never "silent"
            return None
        messages = response.get("messages")
        if not isinstance(messages, list):
            return None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if _reacted(message, only_user=only_user, exclude_user=exclude_user):
                return True
            if not _is_human(message):
                continue
            user = str(message.get("user") or "")
            if only_user:
                if user != only_user:
                    continue
            elif exclude_user and user == exclude_user:
                continue
            try:
                when = float(str(message.get("ts") or ""))
            except (TypeError, ValueError):
                continue
            if when > floor:
                return True
        # SLACK RETURNS REPLIES OLDEST FIRST, so an unread page is the RECENT end of
        # the thread — precisely where an answer would be. Stopping here and reporting
        # silence is how a 201-message thread produced a false accusation.
        if not response.get("has_more"):
            return False
        cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            return None
    return None


def is_member(client: WebClient | None, channel: str, user: str) -> bool | None:
    """Whether `user` is in `channel`, or None when it cannot be determined.

    AN ESCALATION THE ADDRESSEE CANNOT SEE IS THE WORST POSSIBLE TRADE: a colleague is
    publicly named as unresponsive, the manager it was written for never sees it, and
    the worker reports success. A mention only notifies a member — Slack does not
    deliver `<@U…>` to somebody who is not in the conversation.

    None is transient by design, like an unreadable thread: an API hiccup must not
    permanently retire a subject, so callers suppress without recording.
    """
    if client is None or not channel or not user:
        return None
    cursor = ""
    for _ in range(MAX_PAGES):
        try:
            response = client.conversations_members(
                channel=channel,
                limit=PAGE_SIZE,
                **({"cursor": cursor} if cursor else {}),
            )
        except Exception:  # noqa: BLE001 — unknown membership, never a guess
            return None
        members = response.get("members")
        if not isinstance(members, list):
            return None
        if user in {str(item) for item in members}:
            return True
        cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            return False
    return None


def _reacted(
    message: dict[str, object], *, only_user: str = "", exclude_user: str = ""
) -> bool:
    """Whether a human put an emoji on this message.

    `grant.py` calls a reaction "the cheapest +1 there is" when deciding engagement,
    and the escalation guard ignored them entirely — so a card somebody had visibly
    acknowledged could still be reported to a manager as untouched. The reactions were
    already inside the payload this module had fetched and thrown away.
    """
    reactions = message.get("reactions")
    if not isinstance(reactions, list):
        return False
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        users = reaction.get("users")
        if not isinstance(users, list):
            continue
        for raw in users:
            user = str(raw or "")
            if not user:
                continue
            if only_user:
                if user == only_user:
                    return True
            elif user != exclude_user:
                return True
    return False


def _is_human(message: dict[str, object]) -> bool:
    """Whether this message was typed by a person rather than posted by software.

    THE SUBTYPE RULE IS A DENY LIST, NOT "HAS A SUBTYPE". The first version rejected
    any message carrying one, which silently discarded three ordinary human replies —
    `file_share` (a screenshot or the requested spreadsheet), `thread_broadcast` (the
    "also send to channel" checkbox) and `me_message` (`/me`). Each was reproduced
    posting a real accusation about somebody who had in fact answered.

    `bot_id` is absent on messages from Slack APPS posting with a user token; `app_id`
    is what marks the Claude app's own messages, which is why a plain reply sent from
    there does not wake Grant. An EMPTY `bot_id` must not count as a bot — the watchdog
    shipped exactly that, where a falsy id matched every message in the channel.
    """
    if message.get("bot_id") or message.get("app_id"):
        return False
    if str(message.get("subtype") or "") in NON_HUMAN_SUBTYPES:
        return False
    return bool(str(message.get("user") or ""))
