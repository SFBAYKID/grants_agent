"""Keep a human's recent-award constraint through search retries and follow-ups.

Sorting all years newest first failed a real request for awards received recently.
This boundary derives a disclosed default from human words, never model arguments,
and reapplies it before every search so a zero result cannot silently widen it.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any  # Search tool arguments are third-party JSON payloads.

from .source_status import STATE_NAMES

RECENT_AWARD_MONTHS = 6

_RECENT = re.compile(r"\brecent(?:ly)?\b|\bjust\s+(?:got|received|won|awarded)\b")
_AWARD = re.compile(r"\b(?:award\w*|grants?|funding)\b")
_OTHER_MEANING = re.compile(
    r"\b(?:rfps?|solicitations?|opportunit\w*|discover\w*|import\w*|"
    r"spend\w*|deadlines?|news|articles?)\b|grants?\.gov"
)
_EXPLICIT_TIME = re.compile(
    r"\b(?:19|20)\d{2}\b|\b(?:last|past|next|this|previous)\s+"
    r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|sixty|ninety|hundred)\s+){0,3}"
    r"(?:days?|weeks?|months?|years?)\b|"
    r"\b(?:since|before|after|between|during)\b|"
    r"\b(?:in\s+may|may\s+\d+)\b|"
    r"\b(?:january|february|march|april|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b"
)
_HISTORICAL = re.compile(
    r"\b(?:historical|history|older|all\s+(?:years|time)|any\s+(?:year|date)|"
    r"regardless\s+of\s+(?:age|date)|without\s+(?:the\s+)?(?:date|age)\s+"
    r"(?:limit|cutoff|window)|not\s+(?:just\s+)?recent(?:ly)?|ignore\s+(?:the\s+)?"
    r"(?:date|age|recency))\b"
)
_NEGATION = re.compile(
    r"\b(?:not|no|never|don['’]t|didn['’]t|haven['’]t|hasn['’]t|isn['’]t|aren['’]t)\b"
)


def utc_today() -> date:
    """Read one UTC calendar date, independently injectable in regression tests."""
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class RecentAwardScope:
    """The exact inclusive interval chosen for a vague recent-award request."""

    start: date
    end: date

    @property
    def disclosure(self) -> str:
        """State the default and its evidence meaning even when no records match."""
        return (
            f"Recent-award search window: {self.start.isoformat()} through "
            f"{self.end.isoformat()} (the last {RECENT_AWARD_MONTHS} calendar months, "
            "inclusive), using verified announcement/obligation dates, not when "
            "funds arrived. Results cover indexed records only."
        )

    def constrain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Override model omissions or widening while preserving every other filter."""
        return {
            **arguments,
            "record_kind": "award",
            "date_field": "award_received",
            "date_from": self.start.isoformat(),
            "date_to": self.end.isoformat(),
        }


def _continuation(text: str) -> bool:
    """Recognize state/count/format replies without treating a new query as context.

    Only connective vocabulary and state names survive. An organization name,
    program, new date meaning, or substantive new topic stops inheritance. The
    Connecticut spelling is the exact typo in the incident, not a fuzzy matcher.
    """
    remainder = text.lower().replace("conneticut", "connecticut")
    for state in sorted(STATE_NAMES, key=len, reverse=True):
        remainder = re.sub(rf"\b{re.escape(state)}\b", " ", remainder)
    # Codes are explicit uppercase words in the original message; lowercase 'me'
    # and 'in' must remain ordinary connective words rather than geography claims.
    for code in re.findall(r"\b[A-Z]{2}\b", text):
        if code in STATE_NAMES.values():
            remainder = re.sub(rf"\b{code.lower()}\b", " ", remainder)
    tokens = re.findall(r"[a-z]+", remainder)
    allowed = set(
        "and also what how about for in from the same those these them it that this "
        "one ones all both too please thanks thank you yes yep sure ok okay now "
        "can could would will do make put together give show get send export "
        "spreadsheet spreadsheets excel xlsx google sheet sheets file files list "
        "lists results records matches schools school districts district top "
        "of to as a an me us with contacts contact find add instead then only "
        "is fine here slack thread i want need just full complete".split()
    )
    return bool(text.strip()) and all(token in allowed for token in tokens)


def recent_award_scope(
    user_text: str,
    thread_context: list[str] | None,
    *,
    today: date | None = None,
) -> RecentAwardScope | None:
    """Resolve the nearest human recent-award ask through clear continuations only.

    Explicit dates/history and other event meanings supersede the vague default.
    Model/bot words cannot establish or relax it. Incomplete history is discarded
    by the history reader, so an old partial page cannot revive a superseded scope.
    """
    humans = [
        line.partition(":")[2].strip()
        for line in thread_context or []
        if line.startswith("rep:")
    ]
    if humans and humans[-1] == user_text.strip():
        humans.pop()  # Slack history commonly includes the triggering message.
    for text in [user_text, *reversed(humans)]:
        lowered = text.lower()
        if (
            _OTHER_MEANING.search(lowered)
            or _HISTORICAL.search(lowered)
            or _EXPLICIT_TIME.search(lowered)
            or _NEGATION.search(lowered)
        ):
            return None
        if _RECENT.search(lowered) and _AWARD.search(lowered):
            end = today or utc_today()
            month_index = end.year * 12 + end.month - 1 - RECENT_AWARD_MONTHS
            year, month0 = divmod(month_index, 12)
            month = month0 + 1
            start = date(year, month, min(end.day, calendar.monthrange(year, month)[1]))
            return RecentAwardScope(start, end)
        if not _continuation(text):
            return None
    return None


def disclose_recent_search(output: dict[str, Any], disclosure: str) -> dict[str, Any]:
    """Append the executed scope independently of how the model describes results."""
    if disclosure:
        output["reply"] = str(output.get("reply", "")).rstrip() + "\n\n" + disclosure
    return output
