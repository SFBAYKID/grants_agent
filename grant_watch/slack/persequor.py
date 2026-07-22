"""Honest copyable fallback drafting (the send is NEVER Grant's to make).

Honesty rules baked in (CLAUDE.md rule 10): the draft identifies Monarch Connected,
references only award facts we actually hold in the DB, includes an opt-out, and — with
email is not supplied here — carries an explicit RECIPIENT placeholder rather than a
guessed address. The normal path now requests a Persequor draft; this deterministic
copy remains the fallback when its intake is unavailable.

The draft is a deterministic template (testable, no LLM variance). Once the contract
lands, drafting moves to Persequor entirely and this template retires with it.
"""

from __future__ import annotations

import sqlite3

from ..presentation import display_entity_name
from ..record_semantics import RecordKind, semantics_for

SENDER_NAME = "Monarch Connected"


def _amount_phrase(amount: float | None) -> str:
    """Render a sourced dollar amount, or an empty phrase when unknown."""
    return f" ${amount:,.0f}" if amount and amount > 0 else ""


def compose_draft(row: sqlite3.Row) -> str:
    """Deterministic outreach draft from a lead row. Facts only — no invented names,
    dates, or figures; unknown fields degrade gracefully rather than being guessed.

    Wording comes from `record_semantics.semantics_for(row)`, the SAME object
    `persequor_client.build_brief` uses, so the two descriptions of one record cannot
    diverge.

    THIS IS FALLBACK COPY, not an approval preview. `grant.py` calls `build_brief` and
    then `submit_brief` — the POST happens FIRST — and renders this draft only when
    submission failed. On the successful path the rep never sees it. What the rep
    approves is a yes/no question ("Want me to have Persequor draft the intro email?"),
    not this text and not the brief's fields. An earlier docstring called this "the
    draft a human approves", which was untrue and is corrected here.
    """
    entity = display_entity_name(row["entity_name"])
    program = row["program"] or "security"
    meaning = semantics_for(row)
    if meaning.asserts_award:
        amount_context = (
            f" with{_amount_phrase(row['amount'])}" if row["amount"] else ""
        )
        # No raw source key in external copy: "A public seed:svpp_csv record lists …"
        # reached a school administrator once. Internal identifiers never leave.
        fact = (
            f"A public record lists {entity}{amount_context} in {program} funding."
        )
    elif meaning.kind is RecordKind.SOLICITATION:
        fact = f"{entity} published a {program} solicitation."
    elif meaning.kind is RecordKind.FUNDING_OPPORTUNITY:
        fact = f"A public source lists a {program} funding opportunity relevant to {entity}."
    else:
        fact = f"A public source lists {entity} in connection with {program}."
    timing = meaning.outreach_timing(row["funds_end"])
    planning = meaning.planning_clause
    subject_kind = meaning.subject_kind
    return (
        f"To: [RECIPIENT — no verified contact on file; add before sending]\n"
        f"Subject: {program} {subject_kind} at {entity}\n\n"
        f"Hi [NAME],\n\n"
        f"{fact}{timing}\n\n"
        f"I'm reaching out from {SENDER_NAME} — we help schools and cities put security "
        f"funding to work: cameras, access control, and door hardening, handled end to end. "
        f"{planning}, we'd be glad to share a quote or a "
        f"quick walkthrough of what similar districts deployed.\n\n"
        f"If this isn't relevant, reply 'unsubscribe' and we won't contact you again.\n\n"
        f"Best,\n{SENDER_NAME}"
    )


# NOTE: the old Slack-mention handoff was removed because Persequor drops bot messages.
# The replacement is the idempotent HTTP outreach-request contract.
