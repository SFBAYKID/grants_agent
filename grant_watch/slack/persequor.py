"""Outreach drafting + the @Persequor handoff (the send is NEVER Grant's to make).

Honesty rules baked in (CLAUDE.md rule 10): the draft identifies Monarch Connected,
references only award facts we actually hold in the DB, includes an opt-out, and — with
contact enrichment not built yet (Phase 2) — carries an explicit RECIPIENT placeholder
rather than a guessed email address. A human approves in Slack; only then does Grant
post the handoff message that asks @Persequor to send.

The draft is a deterministic template (testable, no LLM variance). A Claude-polish pass
can layer on in Phase 2 once real contact/context data exists to personalize with.
"""

from __future__ import annotations

import os
import sqlite3

SENDER_NAME = "Monarch Connected"


def _amount_phrase(amount: float | None) -> str:
    return f"the {'$' + format(amount, ',.0f')}" if amount else "the"


def compose_draft(row: sqlite3.Row) -> str:
    """Deterministic outreach draft from a lead row. Facts only — no invented names,
    dates, or figures; unknown fields degrade gracefully rather than being guessed."""
    entity = row["entity_name"]
    program = row["program"] or "security"
    window = (f" Our understanding is the spend window runs through {row['funds_end']}."
              if row["funds_end"] else "")
    return (
        f"To: [RECIPIENT — no verified contact on file; add before sending]\n"
        f"Subject: {program} funding at {entity} — implementation help\n\n"
        f"Hi [NAME],\n\n"
        f"Congratulations on {entity}'s {_amount_phrase(row['amount'])} {program} award."
        f"{window}\n\n"
        f"I'm reaching out from {SENDER_NAME} — we help schools and cities put security "
        f"funding to work: cameras, access control, and door hardening, handled end to end. "
        f"If you're planning how to spend this award, we'd be glad to share a quote or a "
        f"quick walkthrough of what similar districts deployed.\n\n"
        f"If this isn't relevant, reply 'unsubscribe' and we won't contact you again.\n\n"
        f"Best,\n{SENDER_NAME}"
    )


def persequor_mention() -> str:
    """A real <@id> mention when PERSEQUOR_USER_ID is configured, else plain text
    (plain '@Persequor' does not ping — set the id in .env to make handoffs ping)."""
    uid = os.environ.get("PERSEQUOR_USER_ID", "")
    return f"<@{uid}>" if uid else "@Persequor"


def build_handoff_text(entity: str, approver: str, draft: str) -> str:
    """The in-thread message that asks Persequor to send the APPROVED draft."""
    return (
        f"{persequor_mention()} — approved by <@{approver}>: please send the email "
        f"below to the appropriate contact at *{entity}*.\n\n```{draft}```"
    )
