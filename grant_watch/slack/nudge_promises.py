"""What Grant may truthfully offer to do next about a lead nobody answered.

WHY THIS IS ITS OWN MODULE. Chase's instruction was "make sure that whatever the agent
is promising it can actually do", prompted by a follow-up that offered to build a
campaign. A proactive message is the worst place to get that wrong: nobody asked for
it, so an offer that cannot be honoured is Grant volunteering a false promise to a
colleague who then has to discover it is empty. The offer therefore comes from the
data, not from a sentence somebody wrote once.

THE PREDICATE IS COPIED FROM THE CONSUMER, NOT INVENTED HERE. `grant._request_outreach`
selects the contact it will actually use with `contact_status == 'verified'`. If this
module offered a draft on the strength of a `vendor_licensed` or `linkedin_only` row,
the offer would read as specific ("want me to draft an intro to Sean Joyce?") and the
acceptance would land in the branch that says it could not verify a contact. Same
predicate on both sides, or the promise and the capability drift apart the first time
either is edited. `test_nudge_followups.py` pins them together.

AND IT NEVER OFFERS TO SEND. Constitution rule 10: a human approves before any email
is sent, and Persequor sends it. "I can email Sean Joyce" would be false in a way that
matters even when the transport is healthy, because the send is not Grant's to make.
"Draft an intro for you to approve" is what actually happens, and it stays true whether
Persequor's intake is up (the brief is submitted) or dark (Grant hands back a copyable
draft) — so the offer does not silently depend on a service this worker cannot see.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .. import db
from ..presentation import strip_leading_honorifics


@dataclass(frozen=True)
class Offer:
    """One concrete next step, and the words for it.

    `kind` is what Grant would do; `question` is how it asks. Both are needed: the
    wording differs per follow-up kind, but the decision about what is TRUE is made
    once, here.
    """

    kind: str  # 'draft_intro' | 'find_email' | 'find_contact'
    question: str
    contact_name: str = ""


def best_offer(conn: sqlite3.Connection, lead_id: int) -> Offer:
    """The most useful thing Grant can honestly promise about this lead right now.

    Degrades rather than overstating: a named contact with a usable email earns a
    specific offer, a named contact without one earns a narrower offer, and no contact
    at all earns the offer Grant can always keep. There is no branch that promises
    something conditional on a service being up.
    """
    if lead_id <= 0:
        return Offer("find_contact", "Want me to find a contact?")
    try:
        rows = list(
            conn.execute(
                "SELECT * FROM contacts "
                "WHERE lead_id=? ORDER BY "
                "CASE contact_status WHEN 'verified' THEN 0 ELSE 1 END, id",
                (lead_id,),
            )
        )
    except sqlite3.Error:
        # A follow-up must never fail because an offer could not be computed; the
        # always-keepable promise is the safe floor.
        return Offer("find_contact", "Want me to find a contact?")

    for row in rows:
        name = _first_name(str(row["name"] or ""))
        if (
            name
            and str(row["email"] or "").strip()
            and db.contact_is_page_verified(row)
        ):
            return Offer(
                "draft_intro",
                f"Want me to draft an intro to {name} for you to approve?",
                contact_name=name,
            )

    for row in rows:
        name = _first_name(str(row["name"] or ""))
        if name:
            return Offer(
                "find_email",
                f"Want me to chase down an email for {name}?",
                contact_name=name,
            )
    return Offer("find_contact", "Want me to find a contact?")


def _first_name(full_name: str) -> str:
    """How a colleague would refer to this person in one line of Slack.

    The card already carries the full name and title; repeating them makes the nudge
    read like a record rather than a person talking. Honorifics are stripped with the
    shared helper so "Dr. Sean Joyce" does not become "Dr.".
    """
    cleaned = strip_leading_honorifics(full_name).strip()
    return cleaned.split()[0] if cleaned else ""
