"""Quote-then-spend ZoomInfo enrichment for one Grant lead.

The whole shape of this module comes from one asymmetry in the vendor's pricing:
finding out WHO exists and WHICH fields they have is free, and only retrieving the
values costs a credit. So a rep is shown a real list and a real price built entirely
from free data, and nothing is billed until they say yes to that exact number.

Two rules this module exists to enforce, both of which are one careless line away
from being violated:
  - Vendor data is never written as a verified contact. It goes in through
    db.save_vendor_contact with its own status, so no `== 'verified'` consumer —
    the Persequor outreach brief above all — can ever pick it up.
  - A do-not-call number is never stored as a phone. It is dropped at the boundary,
    with the flag kept, because every consumer of contacts.phone treats that column
    as dialable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import db
from . import zoominfo, zoominfo_credits

# Titles Grant actually sells to: whoever runs technology, facilities or the money
# at an awardee. Used to narrow a district with hundreds of staff down to the people
# a rep would call. Passing an empty title returns everyone the vendor has.
DECISION_MAKER_TITLES = (
    "technology",
    "information technology",
    "facilities",
    "operations",
    "superintendent",
    "business",
    "security",
)


@dataclass(frozen=True)
class ZoomInfoPreview:
    """What a paid pull WOULD return and cost, built entirely from free search."""

    lead_id: int
    entity_name: str
    matches: tuple[zoominfo.ZoomInfoContactMatch, ...] = field(default=())
    consumed: int = 0
    limit: int = 0
    period: str = ""

    @property
    def billable(self) -> int:
        """Credits this pull would consume (one per record the vendor returns)."""
        return len(self.matches)

    @property
    def remaining(self) -> int:
        """Credits left in the period before this pull."""
        return max(0, self.limit - self.consumed)

    @property
    def affordable(self) -> bool:
        """Whether the period can currently fund the whole pull."""
        return self.billable <= self.remaining

    def summary(self) -> str:
        """One honest paragraph a rep can approve or decline.

        Every number here is measured, not estimated, and the wording never promises
        a value the vendor only claims to hold — `hasEmail` is the vendor's assertion
        about its own data, not evidence the address works.
        """
        if not self.matches:
            return (
                f"ZoomInfo has no contacts on file for {self.entity_name}. "
                "Nothing to pull, and nothing was charged — searching is free."
            )
        quote = zoominfo.quote(list(self.matches))
        lines = [
            f"ZoomInfo lists {self.billable} people at {self.entity_name}. "
            f"Pulling all of them costs {self.billable} of your "
            f"{self.remaining} remaining credits this period ({self.period}).",
            f"Of those: {quote.with_email} have an email on file, "
            f"{quote.with_phone} have a phone number.",
        ]
        if quote.do_not_call:
            lines.append(
                f"{quote.do_not_call} are flagged do-not-call — I will keep the flag "
                "and will not store those numbers."
            )
        lines.append(
            "These names come from ZoomInfo's licensed data, not from the "
            "organization's own website, so I have not verified any of it."
        )
        if not self.affordable:
            lines.append(
                f"This is more than the {self.remaining} credits left, so I can't "
                "run it as-is — narrow it by title or raise the budget."
            )
        return "\n".join(lines)


def preview_for_lead(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    job_title: str = "",
    limit: int = 25,
) -> ZoomInfoPreview:
    """Search ZoomInfo for one lead's organization. FREE — spends no credits."""
    lead = db.get_lead(conn, lead_id)
    if lead is None:
        raise ValueError(f"unknown Grant lead id {lead_id}")
    entity = str(lead["entity_name"] or "")
    matches = zoominfo.search_contacts(
        entity,
        state=str(lead["state"] or ""),
        job_title=job_title,
        limit=limit,
    )
    consumed, ceiling = zoominfo_credits.usage(conn)
    return ZoomInfoPreview(
        lead_id=lead_id,
        entity_name=entity,
        matches=tuple(matches),
        consumed=consumed,
        limit=ceiling,
        period=zoominfo_credits.current_period(),
    )


@dataclass(frozen=True)
class ZoomInfoApplied:
    """The outcome of one paid pull, reconciled against what was actually billed."""

    stored: int
    billed: int
    suppressed_numbers: int
    details: tuple[zoominfo.ZoomInfoContactDetail, ...] = field(default=())

    def summary(self) -> str:
        """Report what was stored and what it cost, without overstating either."""
        if not self.stored:
            return (
                "ZoomInfo returned no usable match, so nothing was stored and "
                "no credit was charged."
            )
        parts = [
            f"Stored {self.stored} contact(s) from ZoomInfo; "
            f"{self.billed} credit(s) were charged."
        ]
        if self.suppressed_numbers:
            parts.append(
                f"{self.suppressed_numbers} phone number(s) were withheld because the "
                "record is flagged do-not-call."
            )
        parts.append("These are vendor-supplied and are not page-verified contacts.")
        return " ".join(parts)


def apply_for_lead(
    conn: sqlite3.Connection,
    lead_id: int,
    person_ids: list[str],
    *,
    requested_by: str = "",
) -> ZoomInfoApplied:
    """Retrieve and store the approved records. THIS SPENDS CREDITS.

    The reservation covers every approved record before the call, so the number the
    rep agreed to is the number at risk, and settlement refunds only what the vendor
    proved was free. Callers must have shown `preview_for_lead(...).summary()` and
    taken an explicit yes first — this function does not ask.
    """
    ids = [pid.strip() for pid in person_ids if pid.strip()]
    if not ids:
        return ZoomInfoApplied(stored=0, billed=0, suppressed_numbers=0)
    # One bounded pull per lead per approved set: re-approving the same people is an
    # explicit new decision, not a silent retry of the old one.
    request_key = f"zoominfo-enrich:{lead_id}:{','.join(sorted(ids))}"

    def work() -> tuple[list[zoominfo.ZoomInfoContactDetail], int]:
        """Call the paid endpoint and report how many records were billable."""
        found = zoominfo.enrich_contacts(ids)
        return found, zoominfo.billable_records(found)

    details = zoominfo_credits.spend(
        conn,
        request_key=request_key,
        credits=len(ids),
        work=work,
        requested_by=requested_by,
        lead_id=lead_id,
    )

    stored = 0
    suppressed = 0
    for detail in details:
        if not detail.matched:
            continue  # NO_MATCH / OPT_OUT are free and assert nothing
        # The two numbers stay APART. Collapsing them with `mobile or direct` put a
        # mobile into a Salesforce Lead's Phone field, which every rep reads as a
        # desk line — the same mistake as putting a switchboard beside a person's
        # name, which this repo has already had to fix once.
        if detail.do_not_call and (detail.mobile_phone or detail.direct_phone):
            suppressed += 1
        db.save_vendor_contact(
            conn,
            lead_id,
            detail.display_name,
            detail.job_title,
            detail.email,
            detail.direct_phone,
            detail.person_id,
            do_not_call=detail.do_not_call,
            mobile_phone=detail.mobile_phone,
        )
        stored += 1
    return ZoomInfoApplied(
        stored=stored,
        billed=zoominfo.billable_records(list(details)),
        suppressed_numbers=suppressed,
        details=tuple(details),
    )
