"""What a proactive follow-up actually SAYS.

Split from `nudges.py` before it reached the 1,000-line cap (CLAUDE.md rule 4). The
boundary is a real one and worth keeping: this module decides WORDING, while
`nudges.py` decides whether, when and to whom. They change for completely different
reasons — a sentence is edited because a rep did not answer it, the worker is edited
because a guard was wrong — and mixing the two is how the A/B variants ended up
comparing a sentence with itself twice.

EVERY SENTENCE HERE IS A CLAIM MADE TO A COLLEAGUE IN A TEAM CHANNEL. Grant sees
Slack and its own tables and nothing else, so none of these may assert what a person
did or did not do: the rep may have phoned the district from the car. They report
what Grant observed and then ask.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from ..presentation import display_entity_name
from . import nudge_promises

if TYPE_CHECKING:  # import-only: the runtime path needs attribute access, not the class
    from .nudge_sources import NudgeCandidate


def build_message(
    candidate: "NudgeCandidate",
    variant: str = "a",
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """One short, human line that is hard to ignore — and still only claims what
    Grant actually observed.

    Length is the whole point. "Scottsbluff Public School still needs follow-up in
    Salesforce" is easy to scroll past; a direct question with a name in it is not.
    Reps were not replying to Grant, so these are written the way a colleague would
    poke you — brief, a little wry, and always ending in something answerable with
    one word. What they must never do is assert what a person did or did not do:
    Grant cannot see a phone call, so it reports its own records and then asks.
    """
    mention = f"<@{candidate.target_slack}> " if candidate.target_slack else ""
    if candidate.subject_kind == "crm_preview_expired":
        if variant == "b":
            return f"{mention}that approval expired before anyone clicked. Rebuild it?"
        return (
            f"{mention}that approval timed out, so nothing got written. "
            "Want me to rebuild it? 🙂"
        )
    if candidate.subject_kind == "crm_batch_blocked":
        count = candidate.observed.get("organizations", 0)
        if variant == "b":
            return (
                f"{mention}{count} orgs here need your call on how to match them. "
                "Add the rest without them?"
            )
        return (
            f"{mention}still stuck on {count} orgs I can't match. "
            "Want me to add the rest?"
        )
    if candidate.subject_kind == "crm_batch_partial":
        if variant == "b":
            return f"{mention}the unmatched ones never made it in. Try them again?"
        return (
            f"{mention}only the ones I could match went in. "
            "Want me to have another go at the rest?"
        )
    if candidate.subject_kind == "capability_now_available":
        return _capability_message(candidate, mention, variant)
    if candidate.subject_kind == "card_escalated":
        return _escalation_message(candidate, mention, variant, conn)
    if candidate.subject_kind == "offer_unanswered":
        return _unanswered_offer_message(candidate, mention, variant)
    if candidate.subject_kind == "thread_abandoned":
        if variant == "b":
            return f"{mention}I dropped the ball here and never answered. Another go?"
        return (
            f"{mention}I never got you an answer on this one. "
            "Want me to pick it back up?"
        )
    entity = display_entity_name(str(candidate.observed.get("entity_name") or ""))
    subject = entity or "that lead"
    amount = int(candidate.observed.get("amount_usd") or 0)
    money = f"${amount:,} " if amount > 0 else ""
    # THE CLOSING OFFER IS COMPUTED PER LEAD. It used to be the fixed sentence "want me
    # to find a contact?", which was weaker than the truth on any lead Grant had
    # already researched — North Palos carried a page-verified Director of Technology
    # and his email at the moment the card posted, and the follow-up still offered to
    # go looking. A specific next step is the whole difference between a message that
    # gets answered and one that gets scrolled past.
    offer = (
        nudge_promises.best_offer(conn, int(candidate.observed.get("lead_id") or 0))
        if conn is not None
        else nudge_promises.Offer("find_contact", "Want me to find a contact?")
    )
    if mention:
        # The card named this person, so the follow-up asks THEM rather than the room.
        # Addressing the channel about a card that pinged one rep produced a
        # follow-up nobody owned, which is how nine cards drew no reply at all.
        if variant == "b":
            # Leads with the MONEY rather than the silence. Which of these gets
            # answered more often is exactly what the variant ledger measures.
            return f"{mention}{money}{subject} is still going spare. {offer.question}"
        # "back here" keeps the claim to what Grant can see: it never asserts the rep
        # did nothing, because they may have phoned the district from the car.
        return f"{mention}nothing back here on {subject} yet. {offer.question}"
    if variant == "b":
        # The untagged wording needed its own alternate too. Without one, the ledger
        # recorded two labels carrying the SAME sentence — and because the whole
        # live queue is untagged cards, `choose` would have declared a winner from
        # pure noise after eight sends. That is the superstition this module's own
        # docstring says it exists to prevent.
        return f"{money}{subject} is still unclaimed. {offer.question}"
    amount_clause = f"{money.strip()}, and " if money else ""
    return (
        f"Anyone want {subject}? {amount_clause}nothing's come back here on it. "
        f"{offer.question}"
    )


# What Grant can now do, phrased as the offer it is. Keyed by capability so the
# sentence stays tied to the thing that actually shipped.
_CAPABILITY_OFFER = {
    "email_results": "I can now — want me to send it?",
    "campaign_load": "I can now — want me to build it?",
    "reminders": "I can now — want me to set it up?",
    "contact_supplied": "I can now — want to send it again?",
}


# The variant-b opener: leads with the capability instead of the apology. Hand
# written per capability, because assembling it from the variant-a fragments
# produced duplicated words and messages that never asked anything.
_CAPABILITY_HEADLINE = {
    "email_results": "I can email you that list now — want it?",
    "campaign_load": "I can build that campaign now — want me to?",
    "reminders": "I can actually track that for you now — set it up?",
    "contact_supplied": "I can save contacts you give me now — want to re-send it?",
}


def _capability_message(
    candidate: "NudgeCandidate", mention: str, variant: str = "a"
) -> str:
    """Reopen an ask Grant had to refuse, quoting the person back to themselves.

    The quote is the evidence. This message makes a claim about something a named
    colleague said weeks ago, and the honest way to make that claim is to show the
    words rather than summarise them — a paraphrase that drifts is Grant putting
    words in someone's mouth, which is rule 1 pointed at a person instead of a lead.
    """
    asked = str(candidate.observed.get("ask_text") or "").strip()
    when = str(candidate.observed.get("asked_on") or "").strip()
    offer = _CAPABILITY_OFFER.get(
        str(candidate.observed.get("capability") or ""),
        "I can do that now — want me to?",
    )
    # SHORT. Chase: keep these like a colleague poking you, not a letter. The
    # verbatim quote is the evidence and stays, but trimmed hard — the permalink in
    # the ledger keeps the full message one click away, so nothing is lost.
    if len(asked) > 70:
        asked = asked[:70].rsplit(" ", 1)[0] + "…"
    opener = f"back on {when}" if when else "a while back"
    # A correction REPLACES "I couldn't do it then". That sentence is true but
    # incomplete where Grant did not merely fail — it said the thing was handled.
    # Reporting only the capability gap would quietly omit the broken promise, which
    # is rule 1 applied to Grant's own conduct rather than to a lead.
    correction = str(candidate.observed.get("correction") or "").strip()
    admission = correction or "I couldn't do it then."
    if variant == "b":
        # LEADS WITH WHAT CHANGED rather than with the apology. Both wordings carry
        # the same quote and the same admission — which one a person actually answers
        # is the question the ledger exists to settle, and it cannot settle anything
        # while both labels carry one sentence.
        #
        # Written out rather than assembled from the variant-a pieces: reordering
        # those fragments produced "I can email you a list now now" and a message
        # that ended without asking anything. A wording a person will read is worth
        # writing by hand.
        headline = _CAPABILITY_HEADLINE.get(
            str(candidate.observed.get("capability") or ""),
            "Good news — I can do that one now.",
        )
        # Upper-case only the FIRST character. `.capitalize()` lower-cases everything
        # after it, which turned "back on 23 July" into "Back on 23 july".
        return (
            f"{mention}{headline} You asked {opener.rstrip(',')}: \u201c{asked}\u201d"
        )
    return f"{mention}{opener} you asked: \u201c{asked}\u201d {admission} {offer}"


def _escalation_message(
    candidate: "NudgeCandidate",
    mention: str,
    variant: str = "a",
    conn: sqlite3.Connection | None = None,
) -> str:
    """Tell a manager one lead went unanswered — briefly, and without accusing anyone.

    Chase asked for this and asked for it SHORT. The care needed is in what it does
    not say: Grant sees Slack and its own tables, so "nothing's come back here" is
    true and "she never followed up" is not — the rep may have phoned the district
    from the car. Naming the money and the person is the point (it is what makes the
    message actionable), so the sentence around them has to be exact.

    THE CLOSING OFFER IS COMPUTED, NOT WRITTEN. Chase's example ended "I can easily
    have Sean Joyce send an email. Do you want me to do that?" — the right instinct,
    and the wrong sentence twice over: Grant does not send prospect email (a human
    approves and Persequor sends, Constitution rule 10), and the specific name is only
    available when a verified contact with an email actually exists on that lead. So
    `nudge_promises.best_offer` reads the lead and returns the strongest promise Grant
    can keep — the named draft when there is a contact to name, a narrower one when
    there is not. Without a connection it degrades to the offer that is always true.

    TWO CASES, AND THE UNTAGGED ONE IS THE ONE THAT PROMPTED THIS. North Palos went out
    to the whole channel and nobody answered; there is no rep to name, and "nobody's
    picked it up" is exactly what a manager needs to hear.
    """
    entity = display_entity_name(str(candidate.observed.get("entity_name") or ""))
    amount = int(candidate.observed.get("amount_usd") or 0)
    who = str(candidate.observed.get("tagged_slack") or "")
    money = f"${amount:,} " if amount > 0 else ""
    subject = entity or "a lead"
    offer = (
        nudge_promises.best_offer(conn, int(candidate.observed.get("lead_id") or 0))
        if conn is not None
        else nudge_promises.Offer("find_contact", "Want me to find a contact?")
    )
    if not who:
        # Nobody was tagged, so nobody can be named. "Here" is load-bearing: the card
        # went to a channel, and Grant can only speak for what came back to it.
        if variant == "b":
            return (
                f"{mention}nobody's picked up {money}{subject} here. "
                f"{offer.question}"
            )
        return (
            f"{mention}heads up — {money}{subject} has been sitting here with no "
            f"takers. Good money, and the window's open. {offer.question}"
        )
    owner = f"<@{who}>"
    if variant == "b":
        return (
            f"{mention}{money}{subject} has been sitting with {owner} — nothing back "
            f"here, though it may be offline. {offer.question}"
        )
    # "nothing back here" and "may be handled offline" are load-bearing: Grant sees
    # Slack and its own tables, so it must not tell a manager the rep did nothing.
    return (
        f"{mention}heads up — {money}{subject} went to {owner} and nothing's come "
        f"back here. May be handled offline. {offer.question}"
    )


# What Grant offered, in the words a colleague would use to describe it. Keyed by
# capability so the sentence stays tied to the thing that actually shipped, and
# hand-written for the same reason as `_CAPABILITY_HEADLINE`: assembling these from
# fragments produced text no person would say.
#
# TWO GRAMMATICAL FORMS, because one wording needs "I offered to BUILD that campaign"
# and the other needs "hasn't come back about BUILDING that campaign". Deriving the
# second from the first is exactly the fragment-reordering that produced "I can email
# you a list now now" the last time it was tried here.
_OFFER_TO_DO = {
    "email_results": "email that list over",
    "campaign_load": "build that campaign",
    "reminders": "set that reminder up",
    "contact_supplied": "save the contact they gave me",
}

_OFFER_ABOUT = {
    "email_results": "emailing that list over",
    "campaign_load": "building that campaign",
    "reminders": "setting that reminder up",
    "contact_supplied": "saving the contact they gave me",
}


def _unanswered_offer_message(
    candidate: "NudgeCandidate", mention: str, variant: str = "a"
) -> str:
    """Tell the manager that an offer Grant made was never answered.

    THE GAP CHASE NAMED: "if Jocelyn does not respond, and let's say after 24 to 30
    hours, the system messages in the main Monarch Cloud Team channel and says
    something like, Hey @Anthony, @Jocelyn has not responded to me."

    This is the ONE follow-up whose central claim is about a specific person's silence,
    so it is also the one with the least room for error. It is sayable at all only
    because `nudge_silence.replied_since` asks SLACK — not the local receipts table,
    which is known to undercount — and refuses to send when it cannot get an answer.
    Even so the wording stays inside what was observed: "hasn't come back to me"
    describes Grant's inbox, not the colleague's day. She may well have built the
    campaign by hand.
    """
    silent = str(candidate.observed.get("silent_slack") or "")
    who = f"<@{silent}>" if silent else "whoever I asked"
    capability = str(candidate.observed.get("capability") or "")
    when = str(candidate.observed.get("asked_on") or "").strip()
    # The original ask date is what makes this worth raising rather than nagging: the
    # request is old, the answer is new, and it is still sitting there. It goes at the
    # END, where it reads as the reason this matters rather than as an aside.
    since = f" — they first asked back on {when}" if when else ""
    if variant == "b":
        about = _OFFER_ABOUT.get(capability, "something I offered to do")
        return f"{mention}{who} hasn't come back to me about {about}. Any ideas?"
    todo = _OFFER_TO_DO.get(capability, "do something they'd asked for")
    return (
        f"{mention}I offered to {todo} for {who} and nothing's come back here{since}. "
        "Worth a poke from you, or shall I leave it?"
    )
