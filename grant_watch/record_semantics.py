"""THE single source of truth for what a lead record actually IS.

Why this module exists (Chase, 2026-07-22, after a critic review): the meaning of a
record — award vs solicitation vs opportunity, and therefore what its dates MEAN — was
derived independently in five places. Two of them derived it from `lead_grade`, which is
a PRIORITY signal, not a statement of fact. When undated California awards were
correctly regraded GOLD→SILVER, those two places began describing real awards as
"published solicitations" and relabelling an award's spend-window end as a "response
deadline" — in Slack, in exports, in a permanent Salesforce note, and in outbound email
to a school administrator.

Patching each copy is how you end up with five copies. The rule now:

    GRADE decides PRIORITY. EVENT TYPE decides WHAT HAPPENED. Never the reverse.

Every consumer routes through `semantics_for(row)`. Adding a sixth consumer means using
this helper, not writing a sixth `if grade == "silver"`.

An unknown or merely-observed event asserts NOTHING — no award, no solicitation, no
date purpose. `record_observed` is what migration 6 backfilled onto pre-existing leads,
so it is a common shape, not an exotic one: being vague there is correct, and the real
remedy is reconstructing those event types from stored evidence (a separate, gated,
production-data question).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum


class RecordKind(str, Enum):
    """Canonical record meanings. Values are the strings search/export already emit."""

    AWARD = "award"
    FUNDING_OPPORTUNITY = "funding_opportunity"
    SOLICITATION = "solicitation"
    UNKNOWN = "watch"


@dataclass(frozen=True)
class RecordSemantics:
    """Every human-facing consequence of one record's kind, in one immutable object.

    The fallback draft and the outbound payload read the SAME instance, so the two
    descriptions of one record cannot diverge. NOTE the flow precisely: `build_brief`
    is submitted to Persequor FIRST, and `compose_draft` renders only as fallback copy
    when submission failed — so on the successful path a rep never sees the draft at
    all. An earlier version of this docstring called it "the draft a human approves",
    which is not true and is corrected here.

    `asserts_amount` exists because prose is not the only place a claim can be made:
    handing an LLM drafting agent a program name and a dollar figure IS an award claim,
    however carefully the surrounding prose is hedged.
    """

    kind: RecordKind
    noun: str  # "funding award", "solicitation", "funding opportunity", "public record"
    entity_role: str  # what the organization IS relative to this record
    angle: str  # the one-line description sent to Persequor
    subject_kind: str  # the noun used in an email subject line
    planning_clause: str  # the "if you're …" clause in outreach copy
    asserts_award: bool  # may copy say money was awarded?
    asserts_amount: bool  # is `amount` an AWARDED sum we may state as money in hand?
    asserts_dates: bool  # do funds_start/funds_end have a stateable meaning?
    window_noun: str  # "spend window" / "response deadline" / … ; "" when unknown

    def date_context(self, start: str, end: str, event_date: str = "") -> str:
        """Render the record's dates with their TRUE purpose, or refuse to.

        This is the `date_context` column of every export and the tail of every Slack
        search line. When the kind is unknown it says so plainly rather than guessing a
        purpose — `funds_end` means a spend deadline, a response deadline or an
        application deadline depending on the kind, and picking wrong is a false claim.
        """
        start_text, end_text = start or "?", end or "?"
        if self.kind is RecordKind.AWARD:
            prefix = f"award event {event_date}; " if event_date else ""
            return f"{prefix}spend window {start_text} through {end_text}"
        if self.kind is RecordKind.FUNDING_OPPORTUNITY:
            return f"applications open {start_text}; close {end_text}"
        if self.kind is RecordKind.SOLICITATION:
            return f"posted {event_date or start_text}; response due {end_text}"
        return f"recorded window {start_text} through {end_text} (purpose unverified)"

    def outreach_timing(self, funds_end: object) -> str:
        """The dated clause for outreach copy, or '' when no date claim is supportable."""
        value = str(funds_end or "").strip()
        if not value or not self.asserts_dates:
            return ""
        if self.kind is RecordKind.AWARD:
            return f" The record shows a spend window through {value}."
        if self.kind is RecordKind.SOLICITATION:
            return f" The response deadline in the source is {value}."
        return f" The application deadline in the source is {value}."


_AWARD = RecordSemantics(
    kind=RecordKind.AWARD,
    noun="funding award",
    entity_role="award recipient",
    angle="recorded funding award with a spend window in the source record",
    subject_kind="funding",
    planning_clause="If you're planning how to use the funding",
    asserts_award=True,
    asserts_amount=True,
    asserts_dates=True,
    window_noun="spend window",
)
_SOLICITATION = RecordSemantics(
    kind=RecordKind.SOLICITATION,
    noun="solicitation",
    entity_role="posting organization",
    angle="published solicitation with a response window in the source record",
    subject_kind="solicitation",
    planning_clause="If you're evaluating security options for the solicitation",
    asserts_award=False,
    # A solicitation's `amount` is an estimate or a ceiling, never money in hand.
    asserts_amount=False,
    asserts_dates=True,
    window_noun="response deadline",
)
_OPPORTUNITY = RecordSemantics(
    kind=RecordKind.FUNDING_OPPORTUNITY,
    noun="funding opportunity",
    entity_role="funding agency",
    angle="published funding opportunity; application dates are in the source record",
    subject_kind="opportunity",
    planning_clause="If you're evaluating whether the program fits your security plans",
    asserts_award=False,
    asserts_amount=False,
    asserts_dates=True,
    window_noun="application window",
)
# The honest fallback. Claims no award, no solicitation, and no date purpose.
_UNKNOWN = RecordSemantics(
    kind=RecordKind.UNKNOWN,
    noun="public record",
    entity_role="organization",
    angle="public funding signal; confirm status and fit before outreach",
    subject_kind="record",
    planning_clause="If security funding is something you're looking at",
    asserts_award=False,
    asserts_amount=False,
    asserts_dates=False,
    window_noun="",
)

_BY_EVENT_TYPE: dict[str, RecordSemantics] = {
    "award_announced": _AWARD,
    "award_obligated": _AWARD,
    "rfp_posted": _SOLICITATION,
    "application_window_opened": _OPPORTUNITY,
}


def event_type_of(row: sqlite3.Row) -> str:
    """Read `current_event_type`, tolerating a row not joined to its funding event.

    Returns "" for a row without the column — which maps to the UNKNOWN semantics, so a
    caller that forgets the join gets vague-but-true output instead of a crash. It does
    NOT fall back to `lead_grade`; that inference is the defect this module replaced.
    """
    try:
        return str(row["current_event_type"] or "")
    except (IndexError, KeyError):
        return ""


def semantics_for(row: sqlite3.Row) -> RecordSemantics:
    """Return what this record IS, derived only from its verified event type."""
    return _BY_EVENT_TYPE.get(event_type_of(row), _UNKNOWN)


def semantics_for_event_type(event_type: str) -> RecordSemantics:
    """Same mapping for callers holding an event type rather than a row."""
    return _BY_EVENT_TYPE.get(event_type or "", _UNKNOWN)
