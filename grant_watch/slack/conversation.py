"""Grant's conversational brain: LLM-powered thread replies with intent detection.

Reps talk to Grant in plain English under a drip post ("I'll take this", "what's
SVPP?", "send them an email"). One Claude call per message returns BOTH the intent
(so grant.py can act: claim, draft, snooze, bad_lead) and the reply text.

Truth constraint is absolute and enforced two ways: the system prompt forbids
inventing facts, AND the only facts in context are the lead row we actually hold.
Engagement is the optimization target INSIDE that constraint — Grant is told to be
brief, useful, and reply-worthy, never to embellish.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from anthropic import Anthropic

DEFAULT_MODEL = "claude-sonnet-5"

_SYSTEM = """You are Grant, Monarch Connected's grant-lead assistant in Slack. Monarch
sells physical security (cameras, access control, door hardening) to schools and
cities; you surface entities that just won government security funding.

Voice: a sharp, direct colleague. One to three short sentences. No emoji. Help-first —
"how does this help the rep" beats cleverness. You WANT replies (your engagement score
depends on it) but you may never earn them dishonestly.

HARD RULES:
- Use ONLY the facts in the FACTS block. If asked something not in it, say you don't
  have that yet — never guess, never invent contacts, dates, links, or figures.
- You cannot send email. If asked to, the intent is draft_email; your reply must say
  the automated Persequor handoff isn't live yet, and offer the copyable draft instead.
- General knowledge questions (e.g. "what is SVPP?") may use your training knowledge,
  clearly as background, not as claims about this specific lead.

Respond with ONLY a JSON object: {"intent": "...", "reply": "..."}
intent ∈ claim | draft_email | snooze | bad_lead | question | chitchat
- claim: the user is taking ownership of the lead ("I'll take this", "mine")
- draft_email: they want an email drafted/sent to the entity
- snooze / bad_lead: they want it parked or killed (bad_lead: reply should ask why
  in one short sentence if no reason was given)
- question / chitchat: everything else."""


def lead_facts(row: sqlite3.Row | None) -> str:
    """The FACTS block — every field Grant is allowed to assert, nothing more."""
    if row is None:
        return "FACTS: (no lead attached to this thread)"
    fields = {
        "entity": row["entity_name"], "state": row["state"],
        "program": row["program"], "amount_usd": row["amount"],
        "window": f"{row['funds_start']} to {row['funds_end']}",
        "source_link": row["detail_url"] or "(none)",
        "status": row["status"],
        "claimed_by": row["assigned_to"] or "(unclaimed)",
        "grade": row["lead_grade"],
    }
    return "FACTS:\n" + "\n".join(f"- {k}: {v}" for k, v in fields.items())


def respond(user_text: str, row: sqlite3.Row | None,
            thread_context: list[str] | None = None) -> dict[str, Any]:
    """One conversational turn -> {'intent': str, 'reply': str}.

    Falls back to a safe, honest reply if the model output isn't parseable —
    a malformed reply must never turn into a wrong action.
    """
    client = Anthropic()  # ANTHROPIC_API_KEY from env
    context = ("\n\nRecent thread:\n" + "\n".join(thread_context[-6:])
               if thread_context else "")
    msg = client.messages.create(
        model=os.environ.get("GRANT_MODEL", DEFAULT_MODEL),
        max_tokens=400,
        system=_SYSTEM,
        messages=[{"role": "user",
                   "content": f"{lead_facts(row)}{context}\n\nUser says: {user_text}"}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        out = json.loads(raw[start:end])
        intent = out.get("intent", "question")
        reply = str(out.get("reply", "")).strip()
        if intent not in ("claim", "draft_email", "snooze", "bad_lead",
                          "question", "chitchat"):
            intent = "question"
        if reply:
            return {"intent": intent, "reply": reply}
    except (ValueError, json.JSONDecodeError):
        pass
    return {"intent": "question",
            "reply": "I didn't parse that cleanly — mind rephrasing? "
                     "(I can take claims, questions, or email requests here.)"}
