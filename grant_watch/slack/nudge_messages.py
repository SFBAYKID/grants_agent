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

from typing import TYPE_CHECKING

from ..presentation import display_entity_name

if TYPE_CHECKING:  # import-only: the runtime path needs attribute access, not the class
    from .nudges import NudgeCandidate


def build_message(candidate: "NudgeCandidate", variant: str = "a") -> str:
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
            return (
                f"{mention}that Salesforce approval expired before it was clicked — "
                "nothing was written. Shall I rebuild it?"
            )
        return (
            f"{mention}that Salesforce approval timed out before anyone hit the "
            "button, so nothing got written. Want me to rebuild it? 🙂"
        )
    if candidate.subject_kind == "crm_batch_blocked":
        count = candidate.observed.get("organizations", 0)
        if variant == "b":
            return (
                f"{mention}{count} organizations on this one are waiting on a call "
                "from you about how to match them. Shall I add the rest without them?"
            )
        return (
            f"{mention}still stuck on this one — {count} organizations need a call "
            "on how to match them. Want me to skip those and add the rest?"
        )
    if candidate.subject_kind == "crm_batch_partial":
        if variant == "b":
            return (
                f"{mention}the unmatched ones from this batch never made it into the "
                "campaign. Want me to try them again?"
            )
        return (
            f"{mention}we only added the ones I could match here — the rest never "
            "made it. Want me to have another go at them?"
        )
    if candidate.subject_kind == "capability_now_available":
        return _capability_message(candidate, mention, variant)
    if candidate.subject_kind == "card_escalated":
        return _escalation_message(candidate, mention, variant)
    if candidate.subject_kind == "thread_abandoned":
        if variant == "b":
            return (
                f"{mention}I dropped the ball on this one and never got you an "
                "answer. Want me to have another go?"
            )
        return (
            f"{mention}I never got you a proper answer on this one, and it looks like "
            "it stalled there. Want me to pick it back up?"
        )
    entity = display_entity_name(str(candidate.observed.get("entity_name") or ""))
    subject = entity or "that lead"
    if mention:
        # The card named this person, so the follow-up asks THEM rather than the room.
        # Addressing the channel about a card that pinged one rep produced a
        # follow-up nobody owned, which is how nine cards drew no reply at all.
        if variant == "b":
            # Leads with the MONEY rather than the silence. Which of these gets
            # answered more often is exactly what the variant ledger measures.
            amount = int(candidate.observed.get("amount_usd") or 0)
            money = f"${amount:,} " if amount > 0 else ""
            return (
                f"{mention}{money}{subject} is still sitting here — want me to find "
                "you a contact for it?"
            )
        return (
            f"{mention}still nothing back on {subject} — though that's only what I "
            "can see here. Want me to find a contact, or shall I drop it?"
        )
    if variant == "b":
        # The untagged wording needed its own alternate too. Without one, the ledger
        # recorded two labels carrying the SAME sentence — and because the whole
        # live queue is untagged cards, `choose` would have declared a winner from
        # pure noise after eight sends. That is the superstition this module's own
        # docstring says it exists to prevent.
        return (
            f"{subject} is still unclaimed. Shall I track down a contact for it, or "
            "let it go?"
        )
    return (
        f"Anyone want {subject}? Nothing's come back here and I've got no activity "
        "logged on it — though that's only what I can see. I can find a contact or "
        "drop it."
    )


# What Grant can now do, phrased as the offer it is. Keyed by capability so the
# sentence stays tied to the thing that actually shipped.
_CAPABILITY_OFFER = {
    "email_results": "I can email you a list now — want me to send it?",
    "campaign_load": (
        "I can build the Salesforce campaign now and add them for you — want me to?"
    ),
    "reminders": (
        "I can hold on to that now and come back to you — want me to set it up?"
    ),
    "contact_supplied": (
        "I can record what you tell me now, tagged as coming from you — want to "
        "give it to me again?"
    ),
}


# The variant-b opener: leads with the capability instead of the apology. Hand
# written per capability, because assembling it from the variant-a fragments
# produced duplicated words and messages that never asked anything.
_CAPABILITY_HEADLINE = {
    "email_results": "I can email you that list now — want it?",
    "campaign_load": "I can build the Salesforce campaign now — want me to?",
    "reminders": "I can hold on to that for you now — want me to set it up?",
    "contact_supplied": "I can record what you tell me now — want to give it again?",
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
    # Long asks are trimmed at a word boundary; the permalink in the ledger keeps the
    # full message one click away, so nothing is lost by not pasting all of it.
    if len(asked) > 160:
        asked = asked[:160].rsplit(" ", 1)[0] + "…"
    opener = f"back on {when}," if when else "a while back,"
    quoted = f'you asked: "{asked}".' if asked else "you asked me for this."
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
        lead = opener[:1].upper() + opener[1:]
        return f"{mention}{headline} {lead} {quoted} {admission}"
    return f"{mention}{opener} {quoted} {admission} {offer}"


def _escalation_message(
    candidate: "NudgeCandidate", mention: str, variant: str = "a"
) -> str:
    """Tell a manager one lead went unanswered — briefly, and without accusing anyone.

    Chase asked for this and asked for it SHORT. The care needed is in what it does
    not say: Grant sees Slack and its own tables, so "nothing's come back here" is
    true and "she never followed up" is not — the rep may have phoned the district
    from the car. Naming the money and the person is the point (it is what makes the
    message actionable), so the sentence around them has to be exact.
    """
    entity = display_entity_name(str(candidate.observed.get("entity_name") or ""))
    amount = int(candidate.observed.get("amount_usd") or 0)
    who = str(candidate.observed.get("tagged_slack") or "")
    money = f"${amount:,} " if amount > 0 else ""
    owner = f"<@{who}>" if who else "the territory rep"
    subject = entity or "a lead"
    if variant == "b":
        return (
            f"{mention}{money}{subject} has been sitting with {owner} and nothing's "
            "come back here — could well be handled offline. Shall I dig out a "
            "contact?"
        )
    return (
        f"{mention}heads up — {money}{subject} went to {owner} and nothing's come "
        "back here since. Could just be handled offline. Want me to find a contact "
        "and draft something?"
    )
