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

SENDER_NAME = "Monarch Connected"


def _amount_phrase(amount: float | None) -> str:
    """Render a sourced dollar amount, or an empty phrase when unknown."""
    return f" ${amount:,.0f}" if amount and amount > 0 else ""


def _event_type(row: sqlite3.Row) -> str:
    """Read `current_event_type`, tolerating a row that was not joined to its event.

    Both production callers pass a `db.get_lead` row, which joins `funding_events`. A
    caller that passes a bare `leads` row gets "" and therefore the conservative
    wording — never a crash, and never a claim the row cannot support. Deliberately NOT
    a fallback to `lead_grade`: that inference is the bug this replaced.
    """
    try:
        return str(row["current_event_type"] or "")
    except (IndexError, KeyError):
        return ""


def compose_draft(row: sqlite3.Row) -> str:
    """Deterministic outreach draft from a lead row. Facts only — no invented names,
    dates, or figures; unknown fields degrade gracefully rather than being guessed."""
    entity = display_entity_name(row["entity_name"])
    program = row["program"] or "security"
    source = row["source"] or "the public source"
    # Wording follows the EVENT TYPE, never `lead_grade`. Grade says how good the lead
    # is; only the event says what happened. When undated California AWARDS were
    # regraded GOLD→SILVER on 2026-07-22, grade-driven wording would have told a school
    # administrator that they had "published a solicitation" and relabelled their award
    # spend-window end as a "response deadline" — two false claims in a real email.
    event_type = _event_type(row)
    if event_type in {"award_announced", "award_obligated"}:
        amount_context = (
            f" with{_amount_phrase(row['amount'])}" if row["amount"] else ""
        )
        fact = (
            f"A public {source} record lists {entity}{amount_context} "
            f"in {program} funding."
        )
        timing = (
            f" The record shows a spend window through {row['funds_end']}."
            if row["funds_end"]
            else ""
        )
        planning = "If you're planning how to use the funding"
        subject_kind = "funding"
    elif event_type == "rfp_posted":
        fact = f"{entity} published a {program} solicitation."
        timing = (
            f" The response deadline in the source is {row['funds_end']}."
            if row["funds_end"]
            else ""
        )
        planning = "If you're evaluating security options for the solicitation"
        subject_kind = "solicitation"
    elif event_type == "application_window_opened":
        fact = f"A public source lists a {program} funding opportunity relevant to {entity}."
        timing = (
            f" The application deadline in the source is {row['funds_end']}."
            if row["funds_end"]
            else ""
        )
        planning = "If you're evaluating whether the program fits your security plans"
        subject_kind = "opportunity"
    else:
        # Unknown or missing event type. Claim NO award, NO solicitation, NO deadline —
        # `funds_end` means something different in each of those cases, so printing it
        # here would assert a meaning we cannot support.
        fact = (
            f"A public {source} record lists {entity} in connection with {program}."
        )
        timing = ""
        planning = "If security funding is something you're looking at"
        subject_kind = "record"
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
