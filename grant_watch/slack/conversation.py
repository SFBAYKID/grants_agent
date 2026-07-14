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
import sqlite3
from typing import Any

from anthropic import Anthropic

from . import tools

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
  red text is banned. Write /grant status, not a code-styled version.
- When you present a lead's details or several facts, lay them out as short bulleted
  lines with *bold labels*, NOT a paragraph. Blank line between the intro and the
  bullets. Example shape:
      *Mt. Morris Consolidated Schools* (MI)
      • *Award:* $500K — SVPP (School Violence Prevention Program)
      • *Window:* Oct 2025 – Sep 2028 (open now)
      • *Fit:* federal security money — cameras, access control, door hardening
      • *Status:* gold, unclaimed
      Want to jump on it?
- Use Slack bold (*word*) for key numbers and labels. Bullets start with "• ".
- Use a NUMBERED list (1. 2. 3.) when the items are steps or a sequence (e.g. next
  actions); use bullets for parallel facts.
- Write clean, proofread English — correct grammar and spelling, no typos.
- Casual one-off replies stay to a sentence or two — don't bulletize everything.
- Triple-backtick blocks are allowed ONLY for a full email draft, nothing else.
- Conversations live in THREADS. If a rep should follow up, tell them to reply right
  here in the thread.

TOOLS: web_search; query_leads (read-only SQL on your lead database); find_contact
(searches an awardee's real website for a Technology Director / Superintendent /
Principal, storing only emails that appear verbatim on a fetched page); salesforce_lookup
(is this awardee already an Account/Lead/Opportunity in our CRM, and who owns it — with
a clickable link); make_spreadsheet (a real .xlsx attached to your reply). Use them
whenever they'd genuinely help. When a rep asks "who do we contact?", run find_contact
AND salesforce_lookup — if it's already in Salesforce, hand them the link and tell them
who owns it before they reach out. Never invent a link, number, contact, or fact: if a
tool errored or found nothing, say so cheerfully and plainly. Present 'possible' CRM
matches as possible, never asserted.

THE OUTREACH HANDOFF (important): you do NOT write or send the outreach email —
that's Persequor, a separate email agent. Persequor is CALL-ONLY: it only acts when
summoned. You are the guide who directs the rep there. So:
- When a rep is ready to reach out (they claimed it, or they ask about emailing), OFFER
  it as a question: "Want me to have Persequor draft the intro email for you?" That is
  intent offer_persequor. Do NOT call Persequor yet.
- ONLY when the rep clearly says yes to bringing in Persequor (look at the recent
  thread — did you just offer and did they confirm?) use intent draft_email. That is
  the single moment Persequor gets called; its draft card then appears in this thread.
- The rep can also summon Persequor themselves by typing @Persequor — if they did,
  you don't need to act.
Be a helpful guide: after a claim, nudge them toward the next step rather than waiting.

HARD RULES:
- Lead-specific claims come ONLY from the FACTS block and tool results.
- You never send email yourself; the send always goes through Persequor + a human tap.
- General knowledge (e.g. what SVPP is) may come from training, as background.

When you are DONE (after any tool use), your final message must be ONLY this JSON:
{"intent": "...", "reply": "..."}
intent is one of: claim | offer_persequor | draft_email | snooze | bad_lead | question | chitchat
- claim: the user is taking ownership ("I'll take this", "mine")
- offer_persequor: they're interested in outreach but haven't confirmed the handoff —
  you're OFFERING to bring in Persequor (your reply asks the question)
- draft_email: they CLEARLY confirmed bringing in Persequor — call it now
- snooze / bad_lead: park it or kill it (for bad_lead with no reason given, ask why
  in one friendly sentence)
- question / chitchat: everything else.
The reply text goes verbatim to Slack — keep it friendly and backtick-free."""


def lead_facts(row: sqlite3.Row | None) -> str:
    """The FACTS block — every lead-specific field Grant may assert."""
    if row is None:
        return "FACTS: (no lead attached to this thread)"
    fields = {
        "lead_id": row["id"],
        "entity": row["entity_name"], "state": row["state"],
        "program": row["program"], "amount_usd": row["amount"],
        "window": f"{row['funds_start']} to {row['funds_end']}",
        "source_link": row["detail_url"] or "(none)",
        "status": row["status"],
        "claimed_by": row["assigned_to"] or "(unclaimed)",
        "grade": row["lead_grade"],
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
        if intent not in ("claim", "offer_persequor", "draft_email", "snooze",
                          "bad_lead", "question", "chitchat"):
            intent = "question"
        if reply:
            return {"intent": intent, "reply": reply}
    except (ValueError, json.JSONDecodeError):
        pass
    # If the model spoke plain text instead of JSON, pass it through as chat.
    text = raw.strip()
    if text:
        return {"intent": "question", "reply": text[:1500]}
    return {"intent": "question",
            "reply": "Hmm, I fumbled that one — mind rephrasing?"}


def respond(user_text: str, row: sqlite3.Row | None,
            thread_context: list[str] | None = None) -> dict[str, Any]:
    """One conversational turn, with tool use.

    Returns {'intent': str, 'reply': str, 'files': [paths]} — grant.py uploads the
    files into the thread and deletes them afterward.
    """
    client = Anthropic()  # ANTHROPIC_API_KEY from env
    context = ("\n\nRecent thread:\n" + "\n".join(thread_context[-6:])
               if thread_context else "")
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": f"{lead_facts(row)}{context}\n\nUser says: {user_text}",
    }]
    files: list[str] = []
    model = os.environ.get("GRANT_MODEL", DEFAULT_MODEL)

    for _ in range(MAX_TOOL_TURNS):
        msg = client.messages.create(
            model=model, max_tokens=1500, system=_SYSTEM,
            tools=tools.TOOL_SCHEMAS, messages=messages,
        )
        if msg.stop_reason != "tool_use":
            raw = "".join(b.text for b in msg.content if b.type == "text")
            out = _parse_final(raw)
            out["files"] = files
            return out
        # Execute every tool call in this turn and feed results back.
        messages.append({"role": "assistant", "content": msg.content})
        results = []
        for block in msg.content:
            if block.type != "tool_use":
                continue
            text, path = tools.run_tool(block.name, dict(block.input))
            if path:
                files.append(path)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": text})
        messages.append({"role": "user", "content": results})

    return {"intent": "question", "files": files,
            "reply": "That took more digging than I expected and I hit my limit — "
                     "try narrowing the ask and I'll go again."}
