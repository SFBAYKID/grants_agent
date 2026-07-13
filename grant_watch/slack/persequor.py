"""Interim outreach drafting (the send is NEVER Grant's to make).

Honesty rules baked in (CLAUDE.md rule 10): the draft identifies Monarch Connected,
references only award facts we actually hold in the DB, includes an opt-out, and — with
contact enrichment not built yet (Phase 2) — carries an explicit RECIPIENT placeholder
rather than a guessed email address. Reps copy the draft manually until the real
Persequor HTTP contract ships (docs/workflow_design.md §4).

The draft is a deterministic template (testable, no LLM variance). Once the contract
lands, drafting moves to Persequor entirely and this template retires with it.
"""

from __future__ import annotations

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


# NOTE: the old Slack-mention handoff (persequor_mention / build_handoff_text) was
# removed 2026-07-13 — Persequor verifiably drops bot messages, so a mention-based
# handoff can never work. Its replacement is the HTTP outreach-request contract in
# docs/workflow_design.md §4, to be built once Chase approves both sides.
