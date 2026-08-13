"""Buy contacts, in bulk, for leads that have none — with the spend bounded up front.

WHY THIS EXISTS. Chase opened a Salesforce Lead Grant had created and found a name, a
state, and a paragraph of prose: "if you go into the lead or into the campaign, none
of the information is there. No address, no notes… The system should be creating that
data on its own."

Measured on production, 9 of the 13 leads on his August campaign hold nothing but
LastName, State and Description, and `MobilePhone` is empty on all 13. None of that is
a code defect — a real paid pull returns a mobile, and the write path accepts one. The
gap is that buying contacts could only ever happen ONE LEAD AT A TIME, through a Slack
conversation, after a human read a preview and said yes. There was no way to say "fill
these thirteen", so nobody ever did, and 997 of 1000 purchased credits sat unused.

THE SHAPE OF THE SAFETY IS THE BUDGET, NOT A PROMPT. Every run takes a hard credit
ceiling and stops at it. The free search decides what a pull would cost BEFORE any
money moves, so a run that would exceed the ceiling declines that lead rather than
part-buying it. `apply_for_lead` still reserves before calling and settles afterwards,
so the ledger stays truthful even if the vendor dies mid-call.

WHO IT BUYS. Decision-makers only, ranked, and at most `PER_LEAD` of them — the point
is a technology or business decision-maker per organisation, not a roster of every
custodian ZoomInfo happens to know. A lead that already has a verified contact is
skipped outright: paying twice for the same person is the easiest money to waste.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db
from .enrich import zoominfo_credits, zoominfo_enrichment
from .enrich.zoominfo_credits import (
    AlreadySpent,
    BudgetExhausted,
    SpendIndeterminate,
)

# At most this many people per organisation. Two is a decision-maker and a fallback;
# more is a directory, and each one costs a credit.
PER_LEAD = 2

# Titles worth paying for, best first. Matched case-insensitively as substrings, so
# "Chief Technology Officer" and "Director of Technology Services" both land.
DECISION_MAKER_TITLES = (
    "chief technology",
    "chief information",
    "director, technology",
    "director of technology",
    "technology director",
    "it director",
    "director, information",
    "information technology",
    "chief business",
    "chief financial",
    "director, facilities",
    "facilities director",
    "director of facilities",
    "superintendent",
    "business manager",
    "director, purchasing",
)


@dataclass(frozen=True)
class FillOutcome:
    """What one run bought, and what it deliberately did not."""

    considered: int
    filled: int
    skipped_have_contact: int
    skipped_no_match: int
    skipped_budget: int
    credits_spent: int

    def summary(self) -> str:
        """One line an operator can paste into Slack."""
        return (
            f"considered {self.considered}, filled {self.filled}, "
            f"already had a contact {self.skipped_have_contact}, "
            f"no ZoomInfo match {self.skipped_no_match}, "
            f"stopped by budget {self.skipped_budget}, "
            f"credits spent {self.credits_spent}"
        )


def rank_candidates(matches: object) -> list[object]:
    """Order people by how likely they are to sign for cameras, best first.

    A ranking rather than a filter: a small district may have no one carrying a
    technology title at all, and a Superintendent with an email beats nobody. But an
    unranked list would spend the same credit on a lacrosse coach, because ZoomInfo
    returns whoever it has.
    """

    def score(match: object) -> tuple[int, int, float]:
        """Lower sorts first: title rank, then no-email penalty, then accuracy."""
        title = str(getattr(match, "job_title", "") or "").lower()
        rank = len(DECISION_MAKER_TITLES)
        for index, wanted in enumerate(DECISION_MAKER_TITLES):
            if wanted in title:
                rank = index
                break
        has_email = 0 if getattr(match, "has_email", False) else 1
        accuracy = -float(getattr(match, "contact_accuracy_score", 0) or 0)
        return (rank, has_email, accuracy)

    return sorted(list(matches), key=score)  # type: ignore[arg-type]


def _has_contact(conn: sqlite3.Connection, lead_id: int) -> bool:
    """Does this lead already have a contact worth paying nothing more for?

    `verified` and `vendor_licensed` both carry real reachable details. A
    `linkedin_only` or `not_found` row does not, which is exactly the 73% of the
    contacts table holding no email, phone or mobile at all.
    """
    return any(
        str(row["contact_status"] or "") == "vendor_licensed"
        or db.contact_is_page_verified(row)
        for row in db.contacts_for_lead(conn, lead_id)
    )


def fill_contacts(
    conn: sqlite3.Connection,
    lead_ids: list[int],
    *,
    max_credits: int,
    per_lead: int = PER_LEAD,
    dry_run: bool = True,
    requested_by: str = "",
) -> FillOutcome:
    """Buy up to `per_lead` decision-makers for each lead, within `max_credits`.

    The ceiling is checked against the FREE preview before every pull, so the run
    stops cleanly at the budget instead of discovering it mid-purchase. A dry run
    performs the searches — which cost nothing — and reports the exact bill.
    """
    if max_credits < 0:
        raise ValueError("max_credits cannot be negative")
    considered = filled = skipped_have = skipped_none = skipped_budget = 0
    spent = 0

    for lead_id in lead_ids:
        considered += 1
        if _has_contact(conn, lead_id):
            skipped_have += 1
            continue
        try:
            preview = zoominfo_enrichment.preview_for_lead(conn, lead_id)
        except ValueError:
            skipped_none += 1
            continue
        chosen = rank_candidates(preview.matches)[:per_lead]
        if not chosen:
            skipped_none += 1
            continue
        cost = len(chosen)
        # THE BUDGET DECIDES BEFORE THE MONEY MOVES. Checking after the call would
        # mean the ceiling is a report rather than a limit.
        if spent + cost > max_credits:
            skipped_budget += 1
            continue
        if dry_run:
            spent += cost
            filled += 1
            continue
        try:
            applied = zoominfo_enrichment.apply_for_lead(
                conn,
                lead_id,
                [str(getattr(m, "person_id", "")) for m in chosen],
                requested_by=requested_by,
            )
        except AlreadySpent:
            # A ROUTINE RE-ENTRY, NOT AN ERROR — the same shape as a Salesforce id
            # from another org. This exact set was already paid for, so the ledger is
            # right to refuse; what was wrong is that the refusal escaped the loop.
            #
            # It is reachable and it compounds: a lead whose chosen people all return
            # NO_MATCH bills nothing and stores nothing, so `_has_contact` stays
            # False, so the next run re-selects the SAME top two (the ranking is
            # deterministic), rebuilds the identical request key, and raises. One
            # such lead would abort every future batch containing it — after the
            # earlier leads in that batch had already been bought and billed. The rep
            # sees a failure, and the money is gone.
            skipped_none += 1
            continue
        except (BudgetExhausted, SpendIndeterminate) as exc:
            # THE SIBLINGS MATTER AS MUCH AS THE ONE I CAUGHT, and I missed them the
            # first time. Both are raised by the same `spend` call. Either escaping
            # discards the whole FillOutcome after earlier leads were already bought
            # and billed — the exact defect the AlreadySpent handler was added to
            # prevent, still live through two other doors.
            #
            # Exhausted means the PERIOD is spent and every later lead would fail the
            # same way, so stop. Indeterminate means one pull's outcome is unknown and
            # must never be silently retried, so stop there too and report what is
            # certain.
            skipped_budget += 1
            _ = exc
            break
        spent += int(applied.billed)
        if applied.stored:
            filled += 1
        else:
            skipped_none += 1

    return FillOutcome(
        considered=considered,
        filled=filled,
        skipped_have_contact=skipped_have,
        skipped_no_match=skipped_none,
        skipped_budget=skipped_budget,
        credits_spent=spent,
    )


def remaining_credits(conn: sqlite3.Connection) -> int:
    """Credits left this period, so a caller can size a run honestly."""
    return zoominfo_credits.remaining(conn)
