"""Shared human-facing formatting for Grant's Slack and outreach surfaces.

Source records retain their original values for matching and audit. These helpers
produce clean, inert display text without changing stored organization identities.
"""

from __future__ import annotations

import re

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
_STATE_DISPLAY_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "Washington, D.C.", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


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
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "rev", "hon", "fr", "sr",
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
