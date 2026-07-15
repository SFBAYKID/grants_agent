"""Shared human-facing formatting for Grant's Slack and outreach surfaces.

Source records retain their original values for matching and audit. These helpers
produce clean, inert display text without changing stored organization identities.
"""

from __future__ import annotations

import re

_ENTITY_ACRONYMS = {
    "ABC", "CCSD", "CSD", "DC", "ISD", "JUSD", "K-12", "LEA", "RSD", "SD",
    "STEAM", "STEM", "UHSD", "USD",
}
_ENTITY_CONNECTORS = {"and", "at", "by", "for", "in", "of", "on", "the", "to"}


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
