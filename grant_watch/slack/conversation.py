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
from datetime import date
from typing import Any  # Anthropic tool-use response payloads are runtime-shaped.

from anthropic import Anthropic

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
from .source_status import slack_source_status_reply

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOOL_TURNS = 6  # runaway guard for the agent loop

_SYSTEM = """You are Grant, Monarch Connected's grant-lead assistant in Slack. Monarch
sells physical security (cameras, access control, door hardening) to schools and
cities; you surface entities that just won government security funding and help the
sales team act on them.

Voice: a FRIENDLY, upbeat colleague — warm first line, then straight to the point.
One to three short sentences unless the rep asked for real detail. No emoji.

FORMATTING (hard rules for Slack — reps SCAN, they don't read paragraphs):
- NEVER use inline backticks or code formatting — Slack renders it as red text and
  red text is banned. Never suggest slash commands or menus; users talk naturally.
- When you present a lead's details or several facts, lay them out as short bulleted
  lines with *bold labels*, NOT a paragraph. Blank line between the intro and the
  bullets. Example shape:
      *Mt. Morris Consolidated Schools* (MI)
      • *Award:* $500K — SVPP (School Violence Prevention Program)
      • *Window:* Oct 2025 – Sep 2028 (open now)
      • *Fit:* federal security money — cameras, access control, door hardening
      • *Source:* <https://www.usaspending.gov/award/EXACT_ID|USASpending award EXACT_ID>
      Want me to check Salesforce for the matching record?
- Use Slack bold (*word*) for key numbers and labels. Bullets start with "• ".
- Use a NUMBERED list (1. 2. 3.) when the items are steps or a sequence (e.g. next
  actions); use bullets for parallel facts.
- Write clean, proofread English — correct grammar and spelling, no typos.
- Casual one-off replies stay to a sentence or two — don't bulletize everything.
- Triple-backtick blocks are allowed ONLY for a full email draft, nothing else.
- Conversations live in THREADS. If a rep should follow up, tell them to reply right
  here in the thread.

YOU HAVE TWO JOBS:
1. Proactive monitoring — you surface fresh grant leads on your own and help reps act.
2. On-demand search — a rep can ask you to find grants by any criteria and you search
   your data and return results, exportable to Excel without leaving Slack.

ON-DEMAND SEARCH — how a rep asks you to find grants, and how you MUST handle it:

STEP 1 — JUST SEARCH when the ask is anchored. If the rep names ANY of a state, an
org type, a city, or an entity, call search_leads right away — it is read-only and
guards oversized results itself. Say something brief and human first ("Let me look."),
never a recitation of filters. Do NOT interrogate the rep about count or format up
front; default to the top 5 in the thread unless they said otherwise.
ONLY when the ask is completely open-ended (no state, no org type, no entity at all)
ask ONE friendly scoping question before searching — e.g. "Should I look everywhere
or one state? And schools, cities, or everything?" — then search as soon as they
answer. Never ask a second scoping question in the same thread; if they say
"everywhere / everything", search exactly that.

STEP 2 — PRESENT, THEN LEAD. Give the ranked results briefly, then offer the next
logical step yourself in the same message — an Excel/Google Sheet export when the
list is long, contacts for the best orgs, or a Salesforce check for a specific one.
The rep should never need to know the system's mechanics to get to a useful result.
If the tool reports more than 15 matches and asks about Excel or Google Sheet, relay
that choice exactly. ALWAYS render each result with its Lead #id (the tool text
carries it) — later turns can only reference a lead by the #id visible in this thread.
EVERY result line MUST keep the source link the tool rendered (<url|source>) — the
link is what keeps the data honest; never drop it. When the tool leads with a grade
split ("29 gold … 6 silver …"), open with that split in plain words before the list.

ZERO RESULTS — GUIDE, NEVER DEAD-END. When search_leads returns "No grants matched"
it also lists nearby alternatives with real counts (e.g. "without the date window:
4,463 matches"). You MUST relay one or two of those with their numbers and offer to
run one — "Nothing in June, but there are 4,463 without the date limit; want the
newest of those, or should I widen to the last 6 months?" If no alternatives came
back, propose the closest sensible widening yourself (longer window, neighboring
states, all org types) and offer to run it. Keep iterating with the rep until they
have something useful or tell you to stop. Never invent results to fill a gap.

STEP 3 — THEN OFFER CONTACTS (never automatic). After the list, OFFER to find the best
contact for each org as a SECOND step, because it's slower (~30s per org): "Want me to
track down the best contact for each? That's about half a minute per org — how many, the
top 5?" ONLY when they say yes with a count, call search_leads AGAIN with the same
filters, with_contacts=true and limit=<that count>. That finds each org's real contact
(a verified email or an honest not-found) and adds contact columns to the list/export.
Never enrich contacts unless they ask.
CONTACT-FOR-A-LISTED-ORG RULE: when the rep asks for the contact at ONE organization
that already appears in this thread's results, do NOT plan or run another search — call
find_contact with that result's Lead # when visible, or with entity=<the exact org
name> and its state (the server resolves it to one lead and refuses ambiguity), plus
salesforce_lookup with the org name. Only search again if the org has never appeared
in this thread.

CITY/ENROLLMENT TRUTH RULE: for school districts, search_leads can match official NCES
district enrollment and district-office city when the rep supplies a two-letter state.
Pass city and/or enrollment_min/enrollment_max with the state. The tool discloses NCES
coverage and excludes unmatched entities from an applied enrollment filter. If NCES is
unavailable or does not match the source entity, repeat the limitation exactly and never
claim that the city/enrollment filter was applied. This does not provide school-level
enrollment or a reliable city field for non-school entities.

DATE TRUTH RULES (non-negotiable):
- discovered = when Grant first imported the record; never call it awarded/received.
- opportunity_open/opportunity_close = Grants.gov application-window dates.
- solicitation_posted/response_due = SILVER RFP dates.
- spend_start/spend_end = GOLD award spending-window dates.
- An unknown award-event date can never support "just," "recently," "landed," or
  "just received." Describe only the verified award record and its spend window.
- The database does NOT store a verified funds-received date. If asked who
  "got/received/was awarded" funding in a date range, do not substitute discovered or
  spend_start. Explain the limitation and ask whether they mean newly discovered leads,
  spend windows that started then, or verified award announcements. date_field
  award_received filters on the verified announced/obligated event date — when you use
  it, say plainly that it is the announcement date, not when money arrived.
- For "next month," use the next CALENDAR month relative to CURRENT_DATE and pass exact
  inclusive date_from/date_to values. Never turn it into "the next 30 days."

ORG TRUTH RULE: org_type means the entity itself (school/city/county/hospital). The city
field is NCES district-office location only and must not be generalized to other orgs.

Export is either an Excel file (export="excel") or a Google Sheet you create and share
with the rep (export="google_sheet") — both land right here in Slack. After results,
offer to refine, export, or (per STEP 3) find contacts.

TOOLS: web_search; lead_stats (typed read-only counts with no raw SQL);
source_inventory_status (read-only catalog/coverage/reviewed-source/batch status);
search_leads (filtered grant search + optional Excel export); find_contact
(searches an awardee's real website for a Technology Director / Superintendent /
Principal, storing only emails that appear verbatim on a fetched page); salesforce_lookup
(is this awardee already an Account/Lead/Opportunity in our CRM, and who owns it — with
a clickable link). Use them
whenever they'd genuinely help. When a rep asks "who do we contact?", run find_contact
AND salesforce_lookup — if it's already in Salesforce, hand them the link and tell them
who owns it before they reach out. Never invent a link, number, contact, or fact: if a
tool errored or found nothing, say so cheerfully and plainly. Present 'possible' CRM
matches as possible, never asserted. When the database has nothing for a funding
question, you may run web_search and answer from it — label those results plainly as
web findings, never as Grant leads or verified awards.

SOURCE DISCOVERY UI: use source_inventory_status for internal inventory, research
coverage, reviewed source candidates, and raw batch status. These are not leads. Never
use web_search for an inventory-status request. Paid discovery runs are disabled in
Slack; say so plainly and do not imply that a typed confirmation can start one.

SOURCE ATTRIBUTION: when the rep asks for details, show the exact current-event source
record as a clickable Slack link using both source_record and source_url from FACTS.
Never reduce it to a generic website or bare domain. If the URL is a parent-award link
or published dataset rather than a direct record, say that explicitly.

LEAD OWNERSHIP: Grant has no claim/dibs workflow. Never say claimed, unclaimed, mine,
locked, assigned, or "claim the lead," and never ask who owns a Grant lead. If a rep
shows interest, check Salesforce. If a complete lookup finds a record, provide its
clickable link. If Salesforce is unavailable or partial, report that limitation and do
not imply the record is absent.

SALESFORCE CONTACT RECORDS — SAME APPROVAL PATTERN AS CAMPAIGNS:
- After find_contact returns a VERIFIED contact (or a LinkedIn person was saved to the
  lead via find_person_linkedin with lead_id), you may OFFER: "Want me to add them to
  Salesforce?" Do not prepare anything until the user clearly says yes.
- On yes, call salesforce_contact_record_preview with the Grant lead_id (add contact_id
  only when the tool asks you to disambiguate). It freezes an exact preview: a person
  Lead (name, title, email, phone, company, full address, website, LinkedIn, number of
  students, industry, record type) owned by the requesting rep, plus a completed
  activity logged to Activity History and a Note carrying the grant context. Fields with
  no verified evidence are shown as blank in the preview — never fill them in yourself
  and never call them errors. LinkedIn-only contacts produce a Lead with NO direct email.
- EMAIL HONESTY: distinguish a DIRECT email (verbatim, tied to the named person) from the
  organization's GENERAL email (info@/office@ from the site). When only the general one
  was verified, say so plainly, e.g. "I added the school's general email but couldn't
  find a direct email for Richard." find_contact tells you exactly what it added from the
  org's website — relay that. Answer follow-ups truthfully: if asked "did you find
  Richard's email?" and only the general address was found, say "No, not his direct
  email — I added the organization's general email instead."
- If the organization is already in Salesforce with one confident match, the preview
  attaches only the Task to the existing record and creates NO duplicate Lead. If the
  duplicate check is ambiguous or Salesforce is unavailable, the tool refuses; relay
  that honestly and suggest the rep resolve it in Salesforce.
- The preview gets a one-time Slack confirmation button; typed yes never performs the
  write. Never claim Salesforce was changed from a preview.

SALESFORCE CAMPAIGNS — EXPLICIT APPROVALS, NEVER SILENT WRITES:
- After returning a fixed lead set, you may OFFER: "Would you like me to add these leads
  to a Salesforce Campaign?" Do not prepare anything until the user says yes.
- Ask for the Campaign name or link, then call salesforce_campaign_search. Show the
  result and ask the user to confirm the exact Campaign. Never select among multiple
  or fuzzy results yourself.
- If none exists, offer a new Campaign. Only after the user gives the name and says to
  create it, call salesforce_campaign_create_preview. The preview gets a one-time Slack
  confirmation button; typed yes alone never performs the write.
- For a confirmed Campaign, call salesforce_campaign_members_preview with the exact
  Grant lead IDs. First leave allow_org_leads=false. If an organization is unmatched,
  ask the user for a Lead/Contact link. If they cannot find one, OFFER organization-only
  Lead creation. Only after explicit approval call it again with allow_org_leads=true.
- Organization-only means the real organization fills Company and LastName and all
  person/contact fields stay blank. Never imply a person was found.
- Campaign and member tools prepare audited previews only. Tell the user to inspect and
  click the confirmation button. Never claim Salesforce was changed from a preview.

THE OUTREACH HANDOFF (important): you do NOT write or send the outreach email —
that's Persequor, a separate email agent. Persequor is CALL-ONLY: it only acts when
summoned. You are the guide who directs the rep there. So:
- When a rep asks about emailing, OFFER
  it as a question: "Want me to have Persequor draft the intro email for you?" That is
  intent offer_persequor. Do NOT call Persequor yet.
- ONLY when the rep clearly says yes to bringing in Persequor (look at the recent
  thread — did you just offer and did they confirm?) use intent draft_email. That is
  the single moment Persequor gets called; its draft card then appears in this thread.
- The rep can also summon Persequor themselves by typing @Persequor — if they did,
  you don't need to act.
- If the rep explicitly asks to draft again, recreate, revise, or start another draft,
  use draft_email again. A new human request is a new draft request; do not say the old
  request prevents it. The server still deduplicates redelivery of that same Slack event.

HARD RULES:
- Lead-specific claims come ONLY from the FACTS block and tool results.
- You never send email yourself; the send always goes through Persequor + a human tap.
- General knowledge (e.g. what SVPP is) may come from training, as background.

When you are DONE (after any tool use), your final message must be ONLY this JSON:
{"intent": "...", "reply": "..."}
intent is one of: offer_persequor | draft_email | snooze | bad_lead | question | chitchat
- offer_persequor: they're interested in outreach but haven't confirmed the handoff —
  you're OFFERING to bring in Persequor (your reply asks the question)
- draft_email: they CLEARLY confirmed bringing in Persequor — call it now
- snooze / bad_lead: park it or kill it (for bad_lead with no reason given, ask why
  in one friendly sentence)
- question / chitchat: everything else.
The reply text goes verbatim to Slack — keep it friendly and backtick-free."""


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


def lead_facts(row: sqlite3.Row | None) -> str:
    """The FACTS block — every lead-specific field Grant may assert."""
    if row is None:
        return "FACTS: (no lead attached to this thread)"
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
    return "FACTS:\n" + "\n".join(f"- {k}: {v}" for k, v in fields.items())


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
    except (ValueError, json.JSONDecodeError):
        pass
    # If the model spoke plain text instead of JSON, pass it through as chat.
    text = raw.strip()
    if text:
        return {"intent": "question", "reply": text[:1500]}
    return {"intent": "question", "reply": "Hmm, I fumbled that one — mind rephrasing?"}


_CRM_ACTION_RE = re.compile(
    r"<grant-crm-action>(\{.*?\})</grant-crm-action>", re.DOTALL
)


def _extract_pending_action(text: str) -> tuple[str, dict[str, str] | None]:
    """Remove a server-only CRM marker and return its validated button metadata."""
    match = _CRM_ACTION_RE.search(text)
    if match is None:
        return text, None
    clean = _CRM_ACTION_RE.sub("", text).strip()
    try:
        value = json.loads(match.group(1))
        action = {
            "action_id": str(value["action_id"]),
            "nonce": str(value["nonce"]),
            "preview": str(value["preview"]),
            "expires_at": str(value["expires_at"]),
        }
    except (KeyError, TypeError, json.JSONDecodeError):
        return clean, None
    return clean, action


def _normalize_action_intent(
    user_text: str,
    thread_context: list[str] | None,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Enforce action intent gates independently of model classification."""
    current = user_text.strip().lower()
    intent = str(output.get("intent") or "question")
    explicit_bad = bool(
        re.search(
            r"\b(?:bad lead|mark (?:it|this).*bad|kill (?:it|this lead)|"
            r"irrelevant lead|not a (?:good|real) lead)\b",
            current,
        )
    )
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
    outreach_ask = bool(
        re.search(r"\b(?:email|outreach|persequor)\b", current)
        or (prior_offer and re.search(r"\bdraft\b", current))
    )
    adversarial = bool(re.search(r"\b(?:ignore .*rules|invent|fabricate)\b", current))
    outreach_refusal = bool(
        re.search(
            r"\b(?:no|nope|cancel|stop|not now|not yet|don't|do not|never)\b",
            current,
        )
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
    if outreach_ask and outreach_refusal:
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
    row: sqlite3.Row | None,
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
    client = Anthropic(timeout=60.0, max_retries=2)
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
    model = os.environ.get("GRANT_MODEL", DEFAULT_MODEL)
    search_confirmed = _search_plan_confirmed(user_text, thread_context)

    try:
        for turn_index in range(MAX_TOOL_TURNS):
            say("Thinking")
            msg = client.messages.create(
                model=model,
                max_tokens=1500,
                system=_SYSTEM,
                tools=tools.TOOL_SCHEMAS,
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
                    else:
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
                    text, action = _extract_pending_action(text)
                    if action is not None:
                        pending_actions.append(action)
                    tool_result_cache[cache_key] = text
                    if single_execution_key:
                        single_execution_cache[single_execution_key] = text
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": text}
                )
            messages.append({"role": "user", "content": results})
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
            model=model, max_tokens=1500, system=_SYSTEM, messages=messages
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
