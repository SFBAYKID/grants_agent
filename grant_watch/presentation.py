"""Shared human-facing formatting for Grant's Slack and outreach surfaces.

Source records retain their original values for matching and audit. These helpers
produce clean, inert display text without changing stored organization identities.
"""

from __future__ import annotations

import re

from .state_codes import US_STATE_NAMES

_ENTITY_ACRONYMS = {
    "ABC",
    "CCSD",
    "CSD",
    "DC",
    "ISD",
    "JUSD",
    "K-12",
    "LEA",
    "RSD",
    "SD",
    "STEAM",
    "STEM",
    "UHSD",
    "USD",
}
_ENTITY_CONNECTORS = {"and", "at", "by", "for", "in", "of", "on", "the", "to"}

# USPS code -> display name. The pollers run nationwide (usaspending.ALL_STATES), so a
# partial map silently degraded real cards to "in TX" / "in KY". Unknown or blank codes
# return "" and the caller omits the location rather than printing a raw code.
_STATE_DISPLAY_NAMES = {**US_STATE_NAMES, "DC": "Washington, D.C."}


def state_display_name(code: object) -> str:
    """Return the spoken state name for a USPS code, or '' when it is not a known state.

    Returning '' for an unknown code is deliberate: a card that cannot name the state
    omits the location instead of printing an unexplained two-letter code at a rep.
    """
    return _STATE_DISPLAY_NAMES.get(str(code or "").strip().upper(), "")


# Honorifics stripped from the front of a person's name so they never become the
# FirstName in Salesforce nor the greeting in an outreach draft (a site listing of
# "Mr. Joel Padgett" must not yield FirstName "Mr. Joel" or a "Hi Mr.," email).
_HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "mx",
    "dr",
    "prof",
    "sir",
    "rev",
    "hon",
    "fr",
    "sr",
}


def strip_leading_honorifics(name: object) -> str:
    """Drop leading honorific tokens (Mr./Mrs./Dr./…) from a person's name.

    Never strips the only remaining token, so 'Dr. Smith' becomes 'Smith' and a
    bare 'Dr.' is returned unchanged. Preserves original spacing otherwise."""
    tokens = str(name or "").split()
    while len(tokens) > 1 and tokens[0].rstrip(".").lower() in _HONORIFICS:
        tokens = tokens[1:]
    return " ".join(tokens)


def plain_fragment(value: object, max_length: int = 120) -> str:
    """Collapse source-controlled text into short, inert conversational prose."""
    text = re.sub(r"(?i)https?://\S+|www\.\S+", "", str(value or ""))
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[.!?]+", "", text)
    inert = text.translate(str.maketrans("", "", "<>@`*_~|")).strip(" ,;:-")
    return inert[:max_length].rstrip(" ,;:-")


# Every Slack markup form that causes a NOTIFICATION, in wire format. Slack stores
# what a person typed, so any text captured from a real message can contain these.
_MENTION_FORMS = (
    # <@U123>, <@U123|name>, <@W123>
    (re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>"), "user"),
    # <!subteam^S123>, <!subteam^S123|@team>
    (re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|([^>]*))?>"), "subteam"),
    # <!here>, <!channel>, <!everyone>, and the |label variants
    (re.compile(r"<!(here|channel|everyone)(?:\|([^>]*))?>"), "broadcast"),
)


def defuse_mentions(value: object, name_for: object = None) -> str:
    """Render Slack mention markup as the words a reader saw, notifying nobody.

    WHY THIS EXISTS. Grant quotes a colleague's own message back to them weeks later —
    "back on 23 July you asked: '…'" — and that quote is stored verbatim from Slack,
    so it holds mentions in WIRE FORMAT. Re-sending it re-fires every one of them: a
    quoted `<!here>` pings the whole channel, and a quoted `<@U…>` pings a third party
    who is not the subject of the follow-up and whose opt-out is never consulted,
    because no code path knows they are named inside a quotation.

    Only the LINK form notifies. Plain "@here" or "@Chase" is inert text, and it is
    also what the original message LOOKED like on screen — so this is the faithful
    rendering as well as the safe one, which matters when the whole justification for
    quoting is showing the words rather than summarising them.

    `name_for` optionally maps a Slack id to a display name; without it an id renders
    as "@someone" rather than leaking a raw identifier into prose a human reads.
    """
    text = str(value or "")
    for pattern, kind in _MENTION_FORMS:

        def replace(match: re.Match[str], kind: str = kind) -> str:
            """One mention, as inert text."""
            label = (match.group(2) or "").strip().lstrip("@")
            if kind == "broadcast":
                return f"@{match.group(1)}"
            if label:
                return f"@{label}"
            if kind == "user" and callable(name_for):
                resolved = name_for(match.group(1))
                if resolved:
                    return f"@{resolved}"
            return "@team" if kind == "subteam" else "@someone"

        text = pattern.sub(replace, text)
    return text


def display_entity_name(value: object, max_length: int = 120) -> str:
    """Humanize all-caps source names while preserving useful education acronyms."""
    entity = plain_fragment(value, max_length=max_length)
    if not entity or any(character.islower() for character in entity):
        return entity
    words: list[str] = []
    for index, word in enumerate(entity.split()):
        bare = word.strip("(),")
        if bare in _ENTITY_ACRONYMS or re.fullmatch(r"[IVX]+", bare):
            formatted = word
        elif index > 0 and bare.lower() in _ENTITY_CONNECTORS:
            formatted = word.lower()
        else:
            formatted = word.title()
        words.append(formatted)
    return " ".join(words)


# Guidance written FOR THE MODEL, wrapped so a human-facing surface can drop it.
# Tool results are read by two very different audiences: the model, which needs to
# be told how to use a result, and — through the reminder worker — a rep, who must
# never see that coaching. A live playground test posted "Offer these to the user
# (with counts) and ask which to run" straight into a Slack thread. Marking the
# guidance is the fix, rather than pattern-matching prose after the fact, because a
# marker cannot drift out of sync with the sentence it wraps.
MODEL_NOTE_OPEN = "<model-note>"
MODEL_NOTE_CLOSE = "</model-note>"
_MODEL_NOTE_RE = re.compile(
    re.escape(MODEL_NOTE_OPEN) + r".*?" + re.escape(MODEL_NOTE_CLOSE), re.DOTALL
)


def model_note(text: str) -> str:
    """Wrap guidance intended only for the model."""
    return f"{MODEL_NOTE_OPEN}{text}{MODEL_NOTE_CLOSE}"


def for_model(text: str) -> str:
    """Tool text as the model should see it: guidance kept, markers removed."""
    return text.replace(MODEL_NOTE_OPEN, "").replace(MODEL_NOTE_CLOSE, "")


def for_human(text: str) -> str:
    """Tool text safe to show a person verbatim: guidance removed entirely.

    Used by surfaces that post a tool result WITHOUT a model rewording it — today
    the reminder worker. Anything that reaches a rep unmediated must come through
    here.
    """
    return _MODEL_NOTE_RE.sub("", text).replace("  ", " ").strip()
