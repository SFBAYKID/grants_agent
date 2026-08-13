"""Grant's conversational brain: an agentic LLM loop with real tools.

Reps talk to Grant in plain English inside threads ("I'll take this", "any news
articles on this district?", "put the WA leads in a spreadsheet"). Grant can search
the web, query its own lead DB, and build spreadsheets — results land back in the
thread (grant.py uploads any files produced).

Truth constraint is absolute: facts come from the FACTS block and tool results only.
Engagement is the optimization target INSIDE that constraint. Slack styling rule from
Chase: NEVER use inline backticks — Slack renders them as red text, and red text is
banned. Friendly, brief, no emoji.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import traceback
from collections.abc import Mapping
from datetime import date
from typing import Any  # Anthropic tool-use response payloads are runtime-shaped.

from anthropic import Anthropic

from ..llm import anthropic_client_options
from ..presentation import display_entity_name
from ..spreadsheets import GeneratedArtifact
from . import tools
from .intent_router import deterministic_reply as _deterministic_reply
from .search_planning import (
    basic_search_arguments as _basic_search_arguments,
)
from .search_planning import (
    finalize_unconfirmed_search_plan as _finalize_unconfirmed_search_plan,
)
from .search_planning import SCOPING_MARKER as _SCOPING_MARKER
from .search_planning import repair_missing_search_plan as _repair_missing_search_plan
from .search_planning import search_confirmation as _search_confirmation
from .search_planning import search_plan_confirmed as _search_plan_confirmed
from .grant_prompt import SYSTEM_PROMPT
from .source_status import slack_source_status_reply

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOOL_TURNS = 6  # runaway guard for the agent loop

_SYSTEM = SYSTEM_PROMPT


def _source_record_label(row: sqlite3.Row) -> str:
    """Describe the exact current event locator without overstating URL precision."""
    source = str(row["source"] or "public source")
    locator = str(row["current_event_source_locator"] or "").strip()
    suffix = f" {locator}" if locator else ""
    if source.startswith("usaspending-subaward:"):
        return f"USASpending subaward{suffix} (URL points to its parent award)"
    if source.startswith("usaspending:"):
        return f"USASpending award{suffix} (direct record)"
    if source == "ca-grants-portal":
        return f"California Grants Portal record{suffix} (published dataset)"
    return f"{source} record{suffix}"


def lead_facts(row: sqlite3.Row | Mapping[str, object] | None) -> str:
    """The FACTS block — every lead-specific field Grant may assert."""
    if row is None:
        return "FACTS: (no lead attached to this thread)"
    keys = set(row.keys())

    def value(name: str, default: object = "") -> object:
        """Read an optional frozen-only fact without weakening legacy rows."""
        return row[name] if name in keys else default

    fields = {
        "lead_id": row["id"],
        "entity": display_entity_name(row["entity_name"]),
        "state": row["state"],
        "program": row["program"],
        "amount_usd": row["amount"],
        "window": f"{row['funds_start']} to {row['funds_end']}",
        "source_record": _source_record_label(row),
        "source_url": row["current_event_source_url"] or "(none)",
        "status": row["status"],
        "grade": row["lead_grade"],
        "event_type": row["current_event_type"],
        "event_date": row["current_event_occurred_on"] or "(unknown)",
        "event_date_precision": row["current_event_date_precision"],
        "event_verification": row["current_event_verification_status"],
        "event_evidence": row["current_event_evidence_excerpt"] or "(none)",
        "salesforce_status": row["salesforce_status"] or "(not checked)",
        "salesforce_opportunity": row["salesforce_opportunity_link"] or "(none)",
        "salesforce_account": row["salesforce_account_link"] or "(none)",
    }
    if "snapshot_contact" in keys:
        fields.update(
            {
                "frozen_public_contact": value("snapshot_contact"),
                "frozen_salesforce_context": value("snapshot_salesforce"),
                "frozen_routing": value("snapshot_routing"),
                "frozen_official_website": value("snapshot_official_website"),
                "snapshot_rule": (
                    "These card facts are FROZEN as posted. Repeat them exactly if "
                    "you repeat them at all. You MAY still look things up in this "
                    "thread — search leads, check Salesforce, find a contact, build "
                    "a campaign — but report whatever you find as CURRENT state "
                    '("as of now Salesforce shows..."), NEVER as a correction to '
                    "the card and never by restating the card's own numbers "
                    "differently. The card recorded what was true when it posted; a "
                    "fresh lookup records what is true now, and both can be right."
                ),
            }
        )
    return "FACTS:\n" + "\n".join(f"- {k}: {v}" for k, v in fields.items())


_REPLY_KEY_RE = re.compile(r'"reply"\s*:\s*"')


def _salvage_truncated_reply(raw: str) -> str:
    """Recover the prose from an envelope the model was cut off part-way through.

    Returns "" when there is nothing recoverable. The caller decides what to do with
    that; this function never invents a completion for the sentence it recovers.
    """
    match = _REPLY_KEY_RE.search(raw)
    if match is None:
        return ""
    decoder = json.JSONDecoder()
    try:
        # Starting AT the opening quote parses a complete string, escapes and all.
        value, _ = decoder.raw_decode(raw[match.end() - 1 :])
        return str(value)
    except json.JSONDecodeError:
        pass
    # Truncated. The cut can land inside an escape (`é`, `\n`), so trim a few
    # characters off the tail until what remains is a decodable string body.
    partial = raw[match.end() :]
    for drop in range(min(8, len(partial)) + 1):
        candidate = partial[: len(partial) - drop].rstrip("\\")
        try:
            return str(json.loads(f'"{candidate}"'))
        except json.JSONDecodeError:
            continue
    return ""


def _looks_like_envelope(raw: str) -> bool:
    """Whether the model was trying to produce the required JSON, not plain prose."""
    head = raw.lstrip().removeprefix("```json").removeprefix("```").lstrip()
    return head.startswith("{") and '"reply"' in raw


_EMPTY_REPLY_FALLBACK = "Hmm, I fumbled that one — mind rephrasing?"

# A sentence end is punctuation FOLLOWED BY WHITESPACE (or the very end). Matching a
# bare "?" cut inside `…/award/ABC123?tab=transactions`, which is worse than a long
# message: Grant's replies carry USASpending verification links, and a link truncated
# at its query string is a dead receipt for a dollar figure. Requiring whitespace
# after the mark also skips the "?" and "!" that appear mid-URL.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


def _sentence_end(text: str) -> int:
    """Index of the last sentence-ending punctuation, or -1.

    Abbreviations are excluded by rejecting a full stop preceded by a single capital
    letter — "the U.S. Department of Justice" must not be cut after "U.S.".
    """
    best = -1
    for match in _SENTENCE_END_RE.finditer(text):
        index = match.start()
        if text[index] == "." and index >= 2 and text[index - 2] == ".":
            continue  # inside an abbreviation like U.S.
        if (
            text[index] == "."
            and index >= 1
            and text[index - 1].isupper()
            and (index < 2 or not text[index - 2].isalpha())
        ):
            continue
        best = index
    return best


# THE SAME 11,000 TOKENS ON EVERY SINGLE CALL. The system prompt is ~6,200 tokens and
# the tool schemas ~5,000, and both are byte-identical every time — re-sent and
# re-billed on every message from every rep, and on every turn of the tool loop.
#
# Anthropic caches a prefix marked with `cache_control`, so the second and subsequent
# calls within the cache window read it instead of reprocessing it. The marker goes on
# the LAST element of each cacheable block, because it caches everything UP TO that
# point: one marker on the final tool covers the whole tool array.
#
# Ordering matters and is fixed by the API: tools are prefixed before system, which is
# prefixed before messages. Anything appended AFTER the marker is uncached, which is
# why the per-turn conversation goes in `messages` and never into `system`.


def _cached_system(memory: str = "") -> list[dict[str, Any]]:
    """The system prompt, plus what Grant remembers about THIS person.

    The order is load-bearing for cost. `_SYSTEM` is identical for everyone and
    carries the cache breakpoint; the memory block varies per colleague and therefore
    goes AFTER it. Putting the per-person text first would change the prefix on every
    turn and defeat caching for every user at once.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": _SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if memory:
        blocks.append({"type": "text", "text": memory})
    return blocks


def _recall_for(slack_user: str) -> str:
    """What Grant knows about this colleague, rendered for the prompt.

    Best-effort by design: a conversation must never fail because the memory store is
    unreachable. Forgetting is a poor turn; erroring is a broken product.
    """
    if not slack_user:
        return ""
    try:
        from .. import db, user_memory

        conn = db.connect_readonly()
        try:
            return user_memory.as_prompt_context(user_memory.recall(conn, slack_user))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — memory is an enhancement, never a dependency
        return ""


def _cached_tools() -> list[dict[str, Any]]:
    """The tool schemas with the cache breakpoint on the last one.

    A COPY is built rather than mutating `tools.TOOL_SCHEMAS`, which is a module-level
    list shared with every other caller and with the tests. Marking it in place would
    make an unrelated import order decide whether a cache_control key exists.
    """
    schemas = [dict(schema) for schema in tools.TOOL_SCHEMAS]
    if schemas:
        schemas[-1] = {**schemas[-1], "cache_control": {"type": "ephemeral"}}
    return schemas


def _parse_final(raw: str) -> dict[str, Any]:
    """Extract the {intent, reply} JSON; degrade to an honest fallback, never to a
    wrong action."""
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        out = json.loads(raw[start:end])
        intent = out.get("intent", "question")
        reply = str(out.get("reply", "")).strip()
        if intent not in (
            "offer_persequor",
            "draft_email",
            "snooze",
            "bad_lead",
            "question",
            "chitchat",
        ):
            intent = "question"
        if reply:
            return {"intent": intent, "reply": reply}
        # A WELL-FORMED ENVELOPE WITH AN EMPTY REPLY WAS NOT TRUNCATED. Falling
        # through to the salvage branch made Grant say "I got cut off before I could
        # finish that one" when nothing had been cut off — a false statement — and
        # threw away a real intent along with it.
        return {"intent": intent, "reply": _EMPTY_REPLY_FALLBACK}
    except (ValueError, json.JSONDecodeError):
        pass
    # A FAILED PARSE HAS TWO CAUSES AND THEY NEED OPPOSITE HANDLING.
    #
    # The model may have spoken plain prose, which is safe to pass through. Or it may
    # have been cut off at the token ceiling part-way through the envelope — and this
    # branch used to pass THAT through too, which is how a rep once received a Slack
    # message beginning `{"intent": "question", "reply": "Both Excel files are done`
    # and ending mid-word. Three other messages reached people cut off mid-sentence
    # the same way. Raw internal scaffolding in a colleague's thread is a product
    # defect; a sentence that stops mid-word is worse, because it looks like an
    # answer and is not one.
    if _looks_like_envelope(raw):
        salvaged = _salvage_truncated_reply(raw).strip()
        if salvaged:
            # Trim to the last completed sentence so nothing ends mid-thought, then
            # say plainly that there was more. Never silently present a fragment as
            # the whole answer.
            cut = _sentence_end(salvaged)
            if cut > 40:
                salvaged = salvaged[: cut + 1]
            return {
                "intent": "question",
                "reply": (
                    f"{salvaged.rstrip()}\n\n"
                    "— I ran out of room mid-answer there. Ask me to carry on and "
                    "I'll pick up where that stops."
                ),
            }
        return {
            "intent": "question",
            "reply": (
                "I got cut off before I could finish that one. Narrow it down a "
                "little and I'll go again."
            ),
        }
    text = raw.strip()
    if text:
        return {"intent": "question", "reply": text[:1500]}
    return {"intent": "question", "reply": "Hmm, I fumbled that one — mind rephrasing?"}


_CRM_ACTION_RE = re.compile(
    r"<grant-crm-action>(\{.*?\})</grant-crm-action>", re.DOTALL
)


def _extract_pending_actions(text: str) -> tuple[str, list[dict[str, str]]]:
    """Remove all server-only CRM markers and return validated button metadata."""
    matches = list(_CRM_ACTION_RE.finditer(text))
    if not matches:
        return text, []
    clean = _CRM_ACTION_RE.sub("", text).strip()
    actions: list[dict[str, str]] = []
    for match in matches:
        try:
            value = json.loads(match.group(1))
            actions.append(
                {
                    "action_id": str(value["action_id"]),
                    "nonce": str(value["nonce"]),
                    "preview": str(value["preview"]),
                    "expires_at": str(value["expires_at"]),
                }
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    return clean, actions


_BAD_LEAD_RE = re.compile(
    r"\b(?:bad lead|mark (?:it|this).*bad|kill (?:it|this lead)|"
    r"irrelevant lead|not a (?:good|real) lead)\b"
)
# A negator immediately before the phrase inverts it: "that's NOT a bad lead".
# Scoped to the text preceding the match so the phrase "not a good lead" — which is
# itself a bad-lead assertion — is not read as its own negation.
_NEGATOR_RE = re.compile(
    r"\b(?:not|isn'?t|wasn'?t|aren'?t|ain'?t|never)\b[^.?!]{0,24}$"
)
# Questions ABOUT a classification, as opposed to requests to apply one. "can you
# mark this a bad lead?" is a request and is deliberately absent from this list.
_QUESTIONING_RE = re.compile(r"^\s*(?:why|what|how|is|are|was|were|did|does|do)\b")


def _asserts_bad_lead(current: str) -> bool:
    """True only when the text ASSERTS a lead is bad, destroying it.

    `bad_lead` sets leads.status='dead' and scores -8, so a false positive silently
    destroys inventory. Matching the phrase alone did exactly that: "that's not a bad
    lead" and "why did you call this a bad lead?" both contain it and both mean the
    opposite, and the override fired even when the model had read the sentence
    correctly. Bias is deliberate — failing to catch a genuine kill request costs one
    repeated message, while a false positive costs a lead nobody can get back.
    """
    match = _BAD_LEAD_RE.search(current)
    if match is None:
        return False
    if _NEGATOR_RE.search(current[: match.start()]):
        return False
    return not _QUESTIONING_RE.match(current)


def _normalize_action_intent(
    user_text: str,
    thread_context: list[str] | None,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Enforce action intent gates independently of model classification."""
    current = user_text.strip().lower()
    intent = str(output.get("intent") or "question")
    explicit_bad = _asserts_bad_lead(current)
    if intent == "bad_lead" and not explicit_bad:
        output["intent"] = "question"
    elif explicit_bad:
        output["intent"] = "bad_lead"
    if intent == "snooze" and not re.search(r"\b(?:snooze|park|hide)\b", current):
        output["intent"] = "question"

    prior_offer = any(
        "grant:" in line.lower()
        and "persequor" in line.lower()
        and re.search(r"\b(?:want|have|bring|draft)\b", line.lower())
        for line in (thread_context or [])[-10:]
    )
    # An outreach ASK is INTENT to send/draft a message to someone — not the noun
    # "email" appearing incidentally. Live bug 2026-07-18: "get me a contact… if
    # there's no email" was misread as a cancellation because it contained both
    # "email" and "no". Require a send/draft verb near email/message, or "email
    # <recipient>", or the outreach/persequor keyword, or a reply to a pending offer.
    outreach_ask = bool(
        re.search(r"\b(?:outreach|persequor)\b", current)
        or re.search(
            r"\b(?:send|draft|write|compose|shoot|fire off|reach out to)\b"
            r"[^.]{0,40}\b(?:e-?mail|intro|message|note)\b",
            current,
        )
        or re.search(
            r"\bemail\s+(?:him|her|them|everyone|all\b|these|those|the\s)", current
        )
        or (prior_offer and re.search(r"\b(?:draft|yes\b|go ahead|do it)\b", current))
    )
    adversarial = bool(re.search(r"\b(?:ignore .*rules|invent|fabricate)\b", current))
    # A refusal only matters as a decline of a PENDING outreach offer. A bare "no"
    # inside a larger request ("…LinkedIn if the site names no one", "no email on
    # file") must NOT cancel anything — live bug 2026-07-18: a City of East
    # Providence contact request was thrown away this way. So require a clear
    # decline token, or a message that is ENTIRELY "no"/"nah".
    outreach_refusal = prior_offer and bool(
        re.search(
            r"\b(?:nope|cancel|stop|not now|not yet|don'?t|do not|never|"
            r"hold off|no thanks|no need)\b",
            current,
        )
        or re.fullmatch(r"\W*(?:no|nah|na)\W*", current)
    )
    explicit_redraft = bool(
        re.search(
            r"\b(?:draft|write|create|make|redo|revise)\b.{0,30}"
            r"\b(?:another|new|again|replacement|revised)\b.{0,30}"
            r"\b(?:email|message|outreach|draft)\b|"
            r"\b(?:another|new|replacement|revised)\b.{0,20}"
            r"\b(?:email|message|outreach|draft)\b",
            current,
        )
    )
    if outreach_refusal:
        # outreach_refusal already requires a pending offer, so this only fires
        # when the user declines it — independent of whether the decline itself
        # reads like an outreach ask.
        output["intent"] = "question"
        output["reply"] = "No problem — I won’t request an outreach draft."
    elif outreach_ask and not adversarial:
        if prior_offer or explicit_redraft:
            output["intent"] = "draft_email"
        else:
            output["intent"] = "offer_persequor"
            boundary = (
                "I don’t send email directly. Want me to have Persequor draft the "
                "intro email for your review?"
            )
            existing = str(output.get("reply") or "").strip()
            claims_send = bool(
                re.search(
                    r"\bsend(?:ing)?\b.{0,30}\bnow\b|\bemail (?:was |has been )?sent\b|"
                    r"\bI(?:'|’)?ve sent\b|\bI sent\b|\bjust sent\b",
                    existing,
                    re.IGNORECASE,
                )
            )
            # Never discard real work: a compound ask ("find X, get the contact,
            # and email them") produces search/contact results in the same reply —
            # keep them and append the email boundary. But a false claim that a
            # send is happening can never survive; it is replaced outright.
            if existing and not claims_send and "persequor" not in existing.lower():
                output["reply"] = existing + "\n\n" + boundary
            elif not existing or claims_send:
                output["reply"] = boundary
    elif intent == "offer_persequor":
        # A model may append a helpful outreach offer after an unrelated answer, but
        # intent drives server behavior and must reflect what the human actually asked.
        output["intent"] = "question"
    return output


def _contextual_tool_error(
    name: str,
    arguments: dict[str, Any],
    row: sqlite3.Row | None,
    user_text: str = "",
) -> str:
    """Reject pronoun-only tool calls when no lead supplies the missing identity."""
    if (
        name == "find_contact"
        and row is None
        and int(arguments.get("lead_id", 0) or 0) <= 0
        and not str(arguments.get("entity", "")).strip()
    ):
        # An explicit lead_id or entity name is allowed even in general threads;
        # wrong ids and ambiguous names fail honestly inside the tool.
        return "ERROR: no lead is attached — ask the user which Lead number they mean."
    if (
        name not in {"salesforce_lookup", "find_person_linkedin", "find_contact"}
        or row is not None
    ):
        return ""
    if int(arguments.get("lead_id", 0) or 0) > 0:
        # An explicit lead binding supplies the identity; a wrong id fails
        # honestly inside the tool instead of being second-guessed here.
        return ""
    entity = str(arguments.get("entity", "")).strip().lower()
    generic = {
        "",
        "it",
        "this",
        "this lead",
        "this one",
        "this organization",
        "this school",
        "current lead",
        "current organization",
        "the organization",
        "the school",
        "unknown",
        "(unknown)",
    }
    stopwords = {
        "account",
        "already",
        "check",
        "current",
        "entity",
        "lead",
        "organization",
        "salesforce",
        "school",
        "this",
    }
    entity_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", entity)
        if len(token) >= 4 and token not in stopwords
    }
    human_tokens = set(re.findall(r"[a-z0-9]+", user_text.lower()))
    if entity in generic or not entity_tokens.intersection(human_tokens):
        return "ERROR: no organization is attached — ask which entity the user means."
    return ""


def _single_execution_tool_key(name: str, arguments: dict[str, Any]) -> str:
    """Identify paid or slow tool modes limited to one execution per human turn."""
    if name == "web_search":
        return "web_search"
    if name == "search_leads" and bool(arguments.get("with_contacts")):
        return "search_leads:with_contacts"
    if name == "fetch_url":
        # Keyed by URL: re-reading the same page is served from cache, and the
        # per-turn fetch budget below bounds how many DISTINCT pages one message
        # can pull. Reading is a paid scrape, and an agent loop with an unbounded
        # reader will happily spend its whole turn budget crawling.
        return f"fetch_url:{str(arguments.get('url', '')).strip().lower()}"
    if name == "email_results":
        # ONE EMAIL PER TURN, whatever the arguments. Every other entry here is keyed
        # so that a genuinely different request may run again; this one is not,
        # because the side effect leaves the system. The agent loop runs up to
        # MAX_TOOL_TURNS with several blocks per turn, so a model that varied the
        # subject line could put six real emails in a colleague's inbox from one
        # sentence — and unlike a repeated search, none of them can be taken back.
        return "email_results"
    if name == "zoominfo_fill_many" and bool(arguments.get("confirm")):
        # ONE PAID BULK PULL PER TURN, whatever the arguments — and the reasoning
        # written above for email applies here with more force, because money is less
        # recoverable than an email.
        #
        # `MAX_CREDITS_PER_CALL` (100) bounds one CALL. It does not bound how many
        # calls a turn may make: six tool turns, several blocks each, and the result
        # cache keyed on exact arguments so varying `lead_ids` defeats it. One rep
        # saying "fill in all the gold leads" could otherwise spend the month.
        return "zoominfo_fill_many:confirm"
    if name == "zoominfo_enrich_contacts":
        # Same rule, smaller blast radius. Keyed without arguments deliberately: a
        # model that varied the person ids would otherwise buy the same organization
        # several times over in one turn.
        return "zoominfo_enrich_contacts"
    return ""


def _ambiguous_award_timing_reply(user_text: str) -> str | None:
    """Reject requests that would confuse award receipt with an indexed date type."""
    lowered = user_text.lower()
    received_language = bool(
        re.search(
            r"\b(?:got|received|won|was awarded|were awarded)\b.{0,40}"
            r"\b(?:grant|grants|funding|award|awards)\b",
            lowered,
        )
    )
    time_language = bool(
        re.search(
            r"\b(?:last|this|past|previous|recent)\s+"
            r"(?:month|week|year|\d+\s+days?)\b|"
            r"\b(?:since|between|during)\b|"
            r"\bin\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
            r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
            r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
            lowered,
        )
    )
    if not (received_language and time_language):
        return None
    # Wording must match what the DB actually holds: no funds-received date exists,
    # but SOME awards carry a verified announcement/obligation event date (what the
    # award_received search sorts and filters on). Offer all three honest readings.
    return (
        "Quick clarification first: Grant never knows when money actually hit an "
        "account. What I can search truthfully is the verified award-announcement "
        "date (where the source recorded one), when a lead was first discovered, or "
        "when an award's spend window started. Which of those do you want for that "
        "time period?"
    )


def respond(
    user_text: str,
    row: sqlite3.Row | Mapping[str, object] | None,
    thread_context: list[str] | None = None,
    on_progress: tools.Progress | None = None,
    requester_slack: str = "",
    workspace: str = "",
    channel: str = "",
    thread_ts: str = "",
) -> dict[str, Any]:
    """One conversational turn, with tool use.

    Returns {'intent': str, 'reply': str, 'files': [GeneratedArtifact]}; grant.py owns
    delivery and cleanup. If the model fails after creating an artifact, this function
    cleans it before re-raising. The dict remains dynamic because Anthropic message
    blocks are third-party runtime objects rather than a stable local model.
    """
    # Loaded once per turn, before any model call, so both the tool loop and the
    # budget-exhausted final answer speak to the same remembered person.
    remembered = _recall_for(requester_slack)
    source_reply = slack_source_status_reply(user_text, thread_context)
    if source_reply is not None:
        return {
            "intent": "question",
            "reply": source_reply,
            "files": [],
            "pending_crm_actions": [],
        }
    # Deterministic router: capability help and simple inventory listings are
    # answered without a model call. Runs after the source-status pre-pass so
    # its richer parsing (and the paid-discovery refusal) always wins.
    routed_reply = _deterministic_reply(user_text, thread_context)
    if routed_reply is not None:
        return {
            "intent": "question",
            "reply": routed_reply,
            "files": [],
            "pending_crm_actions": [],
        }
    timing_reply = _ambiguous_award_timing_reply(user_text)
    if timing_reply is not None:
        return {
            "intent": "question",
            "reply": timing_reply,
            "files": [],
            "pending_crm_actions": [],
        }
    # ANTHROPIC_API_KEY from env. 60s covers a slow tool-planning turn without
    # letting one hung request stall the Slack worker; 2 retries absorb the
    # transient 429/5xx/connection errors that previously surfaced as failures.
    client = Anthropic(**anthropic_client_options())
    say = on_progress or (lambda _msg: None)
    # Keep a wider window so the confirmed filters (STEP 1) survive a few interleaved
    # messages before the rep replies "yes, top 5" (architectural-critic H1).
    context = (
        "\n\nRecent thread:\n" + "\n".join(thread_context[-10:])
        if thread_context
        else ""
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"CURRENT_DATE: {date.today().isoformat()}\n{lead_facts(row)}"
                f"{context}\n\nUser says: {user_text}"
            ),
        }
    ]
    files: list[GeneratedArtifact] = []
    pending_actions: list[dict[str, str]] = []
    # Results (including errors) are cached by name+args only, so a repeat of the
    # IDENTICAL call is served from cache while a corrected call re-executes. A
    # name-keyed error cache proved fatal live: one validation error bricked every
    # corrected retry of that tool and drained the whole turn budget.
    tool_result_cache: dict[str, str] = {}
    single_execution_cache: dict[str, str] = {}
    fetched_pages = 0  # any page reached the context (drives the end-of-turn break)
    paid_fetches = 0  # scrapes actually billed (drives MAX_FETCHES_PER_TURN)
    model = os.environ.get("GRANT_MODEL", DEFAULT_MODEL)
    search_confirmed = _search_plan_confirmed(user_text, thread_context)

    try:
        for turn_index in range(MAX_TOOL_TURNS):
            say("Thinking")
            msg = client.messages.create(
                model=model,
                max_tokens=3000,
                system=_cached_system(remembered),
                tools=_cached_tools(),
                messages=messages,
            )
            if msg.stop_reason != "tool_use":
                raw = "".join(b.text for b in msg.content if b.type == "text")
                if not raw.strip() and turn_index < MAX_TOOL_TURNS - 1:
                    # A transient empty model turn is not a human-facing answer. Give
                    # the same model one bounded chance to satisfy its JSON contract.
                    messages.append({"role": "assistant", "content": msg.content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your response was empty. Complete the user's request "
                                "and return the required intent/reply JSON now."
                            ),
                        }
                    )
                    continue
                out = _finalize_unconfirmed_search_plan(
                    _repair_missing_search_plan(
                        user_text,
                        _normalize_action_intent(
                            user_text, thread_context, _parse_final(raw)
                        ),
                        search_confirmed,
                    ),
                    search_confirmed,
                )
                out["files"] = files
                out["pending_crm_actions"] = pending_actions
                return out
            # Execute every tool call in this turn and feed results back.
            messages.append({"role": "assistant", "content": msg.content})
            results = []
            for block in msg.content:
                if block.type != "tool_use":
                    continue
                if (
                    block.name == "search_leads"
                    and not bool(dict(block.input).get("with_contacts"))
                    and not search_confirmed
                ):
                    # Anchored searches run immediately — they are read-only and
                    # the tool itself guards oversized result sets. Only a fully
                    # open-ended ask (no state/org/city/name anchor) pauses for
                    # ONE scoping question, and never twice in a thread.
                    proposed = _basic_search_arguments(user_text)
                    proposed.update(dict(block.input))
                    anchored = any(
                        str(proposed.get(key) or "").strip()
                        for key in ("state", "org_type", "city", "name_contains")
                    )
                    scoped_already = any(
                        _SCOPING_MARKER.lower() in line.lower()
                        for line in (thread_context or [])[-6:]
                    )
                    if not anchored and not scoped_already:
                        return {
                            "intent": "question",
                            "reply": _search_confirmation(
                                proposed, user_text, thread_context
                            ),
                            "files": files,
                            "pending_crm_actions": pending_actions,
                        }
                tool_args = dict(block.input)
                cache_key = f"{block.name}:{json.dumps(tool_args, sort_keys=True)}"
                # Server-side breadcrumb (bot.log): without it a failed turn leaves
                # no record of which tools ran — proven undiagnosable live.
                print(
                    f"[tool-turn {turn_index}] {cache_key[:300]}",
                    file=sys.stderr,
                    flush=True,
                )
                single_execution_key = _single_execution_tool_key(block.name, tool_args)
                if single_execution_key in single_execution_cache:
                    text = single_execution_cache[single_execution_key]
                elif cache_key in tool_result_cache:
                    text = tool_result_cache[cache_key]
                else:
                    contextual_error = _contextual_tool_error(
                        block.name, tool_args, row, user_text
                    )
                    if contextual_error:
                        text, artifact = contextual_error, None
                    elif (
                        block.name == "fetch_url"
                        and paid_fetches >= tools.MAX_FETCHES_PER_TURN
                    ):
                        # THE PAID-SCRAPE BUDGET, enforced here rather than merely
                        # declared. A turn may emit several fetch_url blocks at once,
                        # and every distinct URL is a billed Firecrawl scrape; the
                        # `break` below only stops the NEXT turn, so without this a
                        # single turn could crawl freely. A cached re-read of a URL
                        # already fetched costs nothing and never reaches here.
                        text, artifact = (
                            "ERROR: I've already read "
                            f"{tools.MAX_FETCHES_PER_TURN} pages for this message. "
                            "Tell me which single link matters most and I'll read "
                            "that one next.",
                            None,
                        )
                    else:
                        if block.name == "fetch_url":
                            paid_fetches += 1
                        text, artifact = tools.run_tool(
                            block.name,
                            tool_args,
                            say,
                            requester_slack=requester_slack,
                            workspace=workspace,
                            channel=channel,
                            thread_ts=thread_ts,
                        )
                    if artifact:
                        files.append(artifact)
                    text, actions = _extract_pending_actions(text)
                    pending_actions.extend(actions)
                    tool_result_cache[cache_key] = text
                    if single_execution_key:
                        single_execution_cache[single_execution_key] = text
                if block.name == "fetch_url":
                    fetched_pages += 1
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": text}
                )
            messages.append({"role": "user", "content": results})
            if fetched_pages:
                # A fetched page is text a stranger wrote. Ending the loop here means
                # nothing inside it can reach another tool — an injected "now create a
                # Salesforce record" has no turn left to be obeyed in.
                break
    except Exception:
        for artifact in files:
            artifact.cleanup()
        raise

    # Tool budget exhausted mid-flow. Instead of a dead-end apology, force ONE
    # final no-tools turn so the user gets an honest summary of what the tools
    # actually returned. The instruction rides in the last tool_result message
    # (a user message may mix tool_result and text blocks).
    try:
        messages[-1]["content"].append(
            {
                "type": "text",
                "text": (
                    "Tool budget for this turn is exhausted; you cannot call more "
                    "tools. Using ONLY the tool results above, give your best "
                    "final answer now: report honestly what was found, say plainly "
                    "what you could not check, and suggest one narrower follow-up. "
                    "Never invent data. Return the required intent/reply JSON."
                ),
            }
        )
        msg = client.messages.create(
            model=model,
            max_tokens=3000,
            system=_cached_system(remembered),
            messages=messages,
        )
        raw = "".join(b.text for b in msg.content if b.type == "text")
        if raw.strip():
            out = _finalize_unconfirmed_search_plan(
                _repair_missing_search_plan(
                    user_text,
                    _normalize_action_intent(
                        user_text, thread_context, _parse_final(raw)
                    ),
                    search_confirmed,
                ),
                search_confirmed,
            )
            out["files"] = files
            out["pending_crm_actions"] = pending_actions
            return out
    except Exception:  # noqa: BLE001 — degraded path; fall back to the honest stub
        print("[tool-error] exhaustion finalizer failed:", file=sys.stderr)
        traceback.print_exc()
    return {
        "intent": "question",
        "files": files,
        "pending_crm_actions": pending_actions,
        "reply": "That took more digging than I expected and I hit my limit — "
        "try narrowing the ask and I'll go again.",
    }
