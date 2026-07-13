"""Lead grading: GOLD / SILVER / WATCH, per Chase's definitions in CLAUDE.md.

Rules (v1 — deliberately simple, tuned as feedback arrives via Grant's [Bad lead] button):
  GOLD    an actual award (money in hand) whose spend window is still open and whose
          amount is positive. Freshness matters: awards started within the last
          FRESH_MONTHS are the hottest; older-but-open awards stay gold (they show up
          in "use-it-or-lose-it" digests) — expired windows drop to watch.
  SILVER  an open RFP/bid matched to our keywords (WEBS, SAM.gov).
  WATCH   everything ambiguous: grants.gov opportunities (pipeline signal, not money),
          negative/zero amounts (de-obligations — found in the 2026-07-13 live run),
          unknown windows. Per CLAUDE.md we keep these rather than drop them.
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import Lead, LeadGrade, RawItem

# Sources whose items are awards (money granted) vs solicitations (RFPs) vs signals.
AWARD_SOURCES_PREFIX = ("usaspending:", "seed:")
RFP_SOURCES = ("webs", "sam.gov")
SIGNAL_SOURCES = ("grants.gov",)

FRESH_MONTHS = 12  # Chase: after ~a year, awardees likely have vendors locked in.


def _parse_date(iso: str) -> date | None:
    """Lenient ISO date parse — sources emit '', full timestamps, or plain dates."""
    if not iso:
        return None
    try:
        return date.fromisoformat(iso[:10])
    except ValueError:
        return None


def grade(item: RawItem, today: date | None = None) -> Lead:
    """Grade one RawItem. Pure function (today injectable) so tests are deterministic."""
    today = today or date.today()

    if item.source.startswith(AWARD_SOURCES_PREFIX):
        end = _parse_date(item.end)
        # De-obligations / zero money are not leads; window closed = vendors done buying.
        if item.amount is not None and item.amount <= 0:
            return Lead(item, LeadGrade.WATCH)
        if end is not None and end < today:
            return Lead(item, LeadGrade.WATCH)
        return Lead(item, LeadGrade.GOLD)

    if item.source in RFP_SOURCES:
        return Lead(item, LeadGrade.SILVER)

    # grants.gov + anything unrecognized: keep as watch, never drop (CLAUDE.md).
    return Lead(item, LeadGrade.WATCH)


def is_fresh(item: RawItem, today: date | None = None) -> bool:
    """True when the award started within FRESH_MONTHS — the 'why now' hook for Slack."""
    today = today or date.today()
    start = _parse_date(item.start)
    return start is not None and start >= today - timedelta(days=FRESH_MONTHS * 30)
