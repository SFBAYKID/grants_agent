"""Read real Slack threads and find the asks Grant could not answer — repeatedly.

WHY THIS EXISTS, IN CHASE'S WORDS: "what you do not want is something hard coded that
fires once and never runs again. Because theoretically the same situation could
happen the next day with a different user, and then we have to go hard code that too."

He was right, and he found the exact seam. Four of the five follow-up kinds already
iterate over live tables and recur forever — an approval that expires tomorrow is
queued tomorrow, a thread Grant drops tomorrow is caught tomorrow. But the fifth,
"you asked for this and I can do it now", was fed by a JSON file a human wrote after
hand-reading July's transcripts. It would have fired once, for five asks, and then
never noticed another one.

This module is the missing half. It reads the channel the way a person would, finds
conversations that ended badly, and asks the model one question about each: did
somebody want something they did not get, and what were their exact words?

THE ANTI-FABRICATION RULE IS MECHANICAL, NOT A PROMPT INSTRUCTION. A model asked to
quote a colleague will sometimes produce a tidier sentence than the one that was
typed. Every quote returned here is checked as a literal substring of a real fetched
message before it is stored; if it does not appear, the finding is dropped and
counted. That check is why `ask_text` can be shown back to a named person weeks later
as "you asked". The model chooses WHAT is interesting; it never authors the evidence.

WHAT IT DELIBERATELY DOES NOT DO: message anybody. Discovery writes an inert row with
`available_since` NULL. A human still runs `capability-available` when a feature has
actually shipped, because the code existing and the team being told are different
decisions — and only the second one pings a real person.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import capability_asks

# How far back a scan looks. Older than this and "you asked me weeks ago" stops
# sounding attentive and starts sounding like surveillance.
LOOKBACK = timedelta(days=45)

# Slack caps a single history page. PAGINATE, because `limit` counts MESSAGES and
# not threads: Grant's channels are shared with other projects' bots, and a single
# 60-message page of a busy channel covered about two days and contained none of
# Grant's own conversations at all. A scan that silently sees nothing reports "no
# unmet asks" — indistinguishable from everything being fine.
PAGE_SIZE = 200
MAX_PAGES = 8

# ONE message is a conversation worth reading. It looks like a stray, but a card
# Grant posted that nobody ever answered is exactly the case Chase asked for —
# "the system posts its morning card and nobody responds, so it then reaches out".
# Setting this to 2 silently discarded every one of them.
MIN_MESSAGES = 1


@dataclass(frozen=True)
class ThreadTranscript:
    """One conversation, as text, with the metadata needed to cite it."""

    channel: str
    thread_ts: str
    messages: tuple[dict[str, Any], ...]

    def human_texts(self) -> tuple[str, ...]:
        """Every message a PERSON wrote. The only text a quote may come from."""
        return tuple(
            str(m.get("text") or "")
            for m in self.messages
            if not m.get("bot_id") and not m.get("app_id") and m.get("user")
        )

    def rendered(self) -> str:
        """The thread as the model will read it, speaker-labelled."""
        lines = []
        for m in self.messages:
            who = "Grant" if (m.get("bot_id") or m.get("app_id")) else "Rep"
            text = str(m.get("text") or "").strip()
            if text:
                lines.append(f"{who}: {text}")
        return "\n".join(lines)

    def grant_took_part(self, grant_user: str, grant_bot: str) -> bool:
        """Did Grant actually participate in this conversation?

        FOUND BY RUNNING THIS LIVE, and it would have been embarrassing. Grant's
        production channel is shared with the Monarch quoting agent, and the recent
        traffic there is mostly people sending IT quote requests — one rep even wrote
        "oops we will make quotes in other channel moving forward!". Without this
        filter the scan read those threads, decided Grant had failed to adjust a
        shipping cost, and would have followed up with a rep about a product Grant
        has nothing to do with.

        A thread counts only if Grant spoke in it or was addressed by name.
        """
        for m in self.messages:
            if grant_bot and str(m.get("bot_id") or "") == grant_bot:
                return True
            if grant_user and grant_user in str(m.get("text") or ""):
                return True
        return False

    def last_speaker_is_human(self) -> bool:
        """True when a person spoke last — i.e. Grant never came back."""
        for m in reversed(self.messages):
            if str(m.get("text") or "").strip():
                return not (m.get("bot_id") or m.get("app_id"))
        return False


def _norm(text: str) -> str:
    """Collapse whitespace so a quote survives Slack's line wrapping."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def quote_is_real(quote: str, transcript: ThreadTranscript) -> bool:
    """Is this quote LITERALLY something a person in this thread typed?

    The one guard that makes a discovered ask safe to repeat back to its author. A
    model summarising "email those to kerry@..." as "send me the list" is a perfectly
    good summary and a completely unacceptable quotation, because Grant will later
    say "you asked" and attach the words to a named colleague.
    """
    needle = _norm(quote)
    if len(needle) < 8:
        return False
    return any(needle in _norm(text) for text in transcript.human_texts())


def canonical_capability(name: str, known: object) -> str:
    """Fold a freshly-invented slug onto one already in use, when they mean the same.

    ALSO FOUND LIVE. Letting the model name the capability is what makes this
    malleable, but unconstrained it produced twelve slugs for one request —
    `adjust_shipping`, `modify_shipping_amount`, `adjust_shipping_cost`,
    `add_shipping_adjustment` and so on. That is worse than the closed enum it
    replaced: `mark_available` keys on the slug, so shipping the feature would arm
    one spelling and leave the other eleven asks silently waiting forever.

    So the vocabulary stays open, and CONVERGES: near-synonym verbs and empty nouns
    are folded out, and a new slug reusing an existing meaning reuses its name. The
    set grows only when something genuinely new appears — which is the "gets smarter
    over time" property rather than a fixed list.
    """
    if not name:
        return ""
    tokens = _content_tokens(name)
    if not tokens:
        return name
    for existing in known or ():
        if _content_tokens(str(existing)) == tokens:
            return str(existing)
    return name


# Verbs that mean the same thing to a rep asking for something.
_SYNONYMS = {
    "modify": "change",
    "adjust": "change",
    "update": "change",
    "edit": "change",
    "set": "change",
    "fix": "change",
    "alter": "change",
    "fetch": "get",
    "pull": "get",
    "retrieve": "get",
    "lookup": "get",
    "send": "email",
    "deliver": "email",
}

# Nouns that carry no meaning on their own and cause spurious distinctions.
_EMPTY = {
    "amount",
    "amounts",
    "cost",
    "costs",
    "value",
    "values",
    "data",
    "info",
    "information",
    "to",
    "from",
    "the",
    "a",
    "for",
    "of",
    "and",
    "in",
    "on",
}


def _content_tokens(slug: str) -> frozenset[str]:
    """The meaning-bearing, synonym-folded, singularised words in a slug."""
    out = set()
    for raw in re.split(r"[_\W]+", slug.lower()):
        if not raw or raw in _EMPTY:
            continue
        word = _SYNONYMS.get(raw, raw)
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]  # campaigns -> campaign, so one ask is not two
        out.add(_SYNONYMS.get(word, word))
    return frozenset(out)


def fetch_threads(
    client: WebClient, channel: str, *, now: datetime | None = None
) -> list[ThreadTranscript]:
    """Pull recent conversations from one channel.

    Reads Slack live rather than a local table on purpose: the record of what a rep
    actually said lives in Slack, and a scanner that only sees Grant's own database
    can only ever find problems Grant already noticed.
    """
    moment = now or datetime.now(timezone.utc)
    oldest = (moment - LOOKBACK).timestamp()
    parents: list[dict[str, Any]] = []
    cursor = ""
    for _page in range(MAX_PAGES):
        try:
            history = client.conversations_history(
                channel=channel,
                oldest=str(oldest),
                limit=PAGE_SIZE,
                **({"cursor": cursor} if cursor else {}),
            )
        except SlackApiError:
            break
        parents.extend(history.get("messages", []) or [])
        cursor = str((history.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            break
    out: list[ThreadTranscript] = []
    for parent in parents:
        ts = str(parent.get("thread_ts") or parent.get("ts") or "")
        if not ts:
            continue
        try:
            replies = client.conversations_replies(channel=channel, ts=ts, limit=100)
        except SlackApiError:
            continue
        messages = tuple(replies.get("messages", []) or ())
        if len(messages) < MIN_MESSAGES:
            continue
        out.append(ThreadTranscript(channel=channel, thread_ts=ts, messages=messages))
    return out


PROMPT = """You are reviewing a Slack conversation between a sales rep and Grant, an \
assistant that finds government grant leads.

Find every case where the REP wanted something and did not get it — Grant said it \
could not do it, gave an answer that did not address the request, or never replied \
at all. Ignore requests Grant actually fulfilled.

Return JSON only: {"asks": [{"quote": "...", "capability": "...", "why": "..."}]}

- "quote" MUST be copied character-for-character from something the Rep typed. Do not \
tidy it, fix spelling, shorten it, or merge two sentences. If you cannot copy it \
exactly, leave the ask out.
- "capability" is a short lowercase slug naming the missing ability, e.g. \
email_results, export_to_sheets, remove_from_campaign. Invent one that fits; do not \
force it into a category.
- "why" is one short sentence on what the rep actually wanted.
- If nothing was left unmet, return {"asks": []}.

Conversation:
"""


def extract_asks(
    transcript: ThreadTranscript, ask_model: Any
) -> tuple[list[dict[str, str]], int]:
    """Ask the model what went unanswered. Returns (verified asks, discarded count).

    `ask_model` is any callable taking a prompt and returning text, so this is
    testable without a network call and the transport can change underneath it.
    """
    try:
        raw = ask_model(PROMPT + transcript.rendered())
    except Exception:  # noqa: BLE001 — a scan must never break the caller
        return [], 0
    match = re.search(r"\{.*\}", str(raw or ""), re.DOTALL)
    if not match:
        return [], 0
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return [], 0
    verified: list[dict[str, str]] = []
    discarded = 0
    for item in parsed.get("asks", []) or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        capability = str(item.get("capability") or "").strip().lower()
        # THE GUARD. A quote the rep did not type is not evidence, however sensible
        # it reads — Grant would later attribute it to them by name.
        if not quote or not quote_is_real(quote, transcript):
            discarded += 1
            continue
        if not capability_asks._SLUG.fullmatch(capability):
            discarded += 1
            continue
        verified.append(
            {
                "quote": quote,
                "capability": capability,
                "why": str(item.get("why") or ""),
            }
        )
    return verified, discarded


def _author_of(quote: str, transcript: ThreadTranscript) -> dict[str, Any] | None:
    """The message a verified quote came from, so the row can cite it."""
    needle = _norm(quote)
    for m in transcript.messages:
        if m.get("bot_id") or m.get("app_id") or not m.get("user"):
            continue
        if needle in _norm(str(m.get("text") or "")):
            return m
    return None


def scan_channel(
    client: WebClient,
    conn: sqlite3.Connection,
    channel: str,
    ask_model: Any,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
    grant_user: str = "",
    grant_bot: str = "",
) -> str:
    """Read a channel's recent threads and record what went unanswered.

    Safe to run repeatedly: `capability_asks.record` is idempotent per person, thread
    and capability, so re-scanning the same month re-finds the same asks and stores
    each one once. That is what makes this a standing job rather than a migration.
    """
    threads = fetch_threads(client, channel, now=now)
    # Only Grant's own conversations. A shared channel carries other bots' traffic,
    # and following up on someone else's product is worse than not following up.
    if grant_user or grant_bot:
        before = len(threads)
        threads = [t for t in threads if t.grant_took_part(grant_user, grant_bot)]
        skipped_foreign = before - len(threads)
    else:
        skipped_foreign = 0
    known = {
        str(r[0])
        for r in conn.execute("SELECT DISTINCT capability FROM capability_asks")
    }
    found = 0
    stored = 0
    dropped = 0
    for transcript in threads:
        asks, discarded = extract_asks(transcript, ask_model)
        dropped += discarded
        for ask in asks:
            # Reuse an existing name when this is the same ask under a new spelling,
            # so shipping the feature arms every row waiting on it.
            ask["capability"] = canonical_capability(ask["capability"], known)
            known.add(ask["capability"])
            source = _author_of(ask["quote"], transcript)
            if source is None:
                dropped += 1
                continue
            found += 1
            if dry_run:
                continue
            result = capability_asks.record(
                conn,
                slack_user=str(source.get("user") or ""),
                audience=channel,
                thread_ts=transcript.thread_ts,
                message_ts=str(source.get("ts") or ""),
                ask_text=ask["quote"],
                capability=ask["capability"],
                asked_at=_ts_to_iso(str(source.get("ts") or "")),
                recorded_by="thread-scan",
            )
            if result is not None:
                stored += 1
    prefix = "[dry-run] " if dry_run else ""
    foreign = (
        f", {skipped_foreign} threads skipped as not Grant's" if skipped_foreign else ""
    )
    return (
        f"{prefix}scanned {len(threads)} threads in {channel}: {found} unmet asks, "
        f"{stored} newly recorded, {dropped} discarded as unverifiable{foreign}"
    )


def _ts_to_iso(ts: str) -> str:
    """Slack's epoch-with-microseconds to the repo's stored form."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()
