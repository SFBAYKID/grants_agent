"""Lead grading: GOLD / SILVER / WATCH, per Chase's definitions in CLAUDE.md.

Rules (Chase, refined 2026-07-19 — grants outrank RFPs; freshness is everything):
  GOLD    an actual award (money in hand), positive amount, spend window still open, AND
          obligated within the last FRESH_MONTHS — a district that just got funded is a
          hot buyer. The same award obligated over a year ago drops to SILVER (they
          likely have vendors locked in). A verified obligation date within a few days
          becomes PLATINUM at the drip layer.
  SILVER  an open RFP/bid (the aggregator, WEBS, SAM.gov, OregonBuys) OR an older-but-
          still-open award. RFPs are SILVER AT BEST, never gold: winning one is a lot of
          work with a low hit rate, so a solicitation never outranks a real award.
  WATCH   everything ambiguous: grants.gov opportunities (pipeline signal, not money),
          negative/zero amounts (de-obligations — found in the 2026-07-13 live run),
          past-due RFPs, unknown windows. Per CLAUDE.md we keep these rather than drop.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from .models import Lead, LeadGrade, RawItem

# Sources whose items are awards (money granted) vs solicitations (RFPs).
AWARD_SOURCES_PREFIX = (
    "usaspending:",
    "usaspending-subaward:",
    "ca-grants-award:",
    "seed:",
)
RFP_SOURCES = ("webs", "sam.gov", "oregonbuys", "rfp")

FRESH_MONTHS = 12  # Chase: after ~a year, awardees likely have vendors locked in.

# THE CEILING ON WHAT GRANT WILL PROACTIVELY SURFACE. Calendar months, inclusive.
#
# GOLD (FRESH_MONTHS) is the grade: "money in hand, obligated within a year". This is
# a stricter rule about what Grant is allowed to PUSH at a person — a card in the
# channel, a name tagged, a manager escalated to. Chase, 2026-09-04, on a nudge that
# tagged the manager about a $499,730 award obligated October 2025: "You're reminding
# everybody of a really really old lead ... We need to be reminding people of the
# newest leads." Every award card the drip had ever posted was between 9 and 21
# months old (see presentation.award_age_phrase), because the only ceiling was the
# grade's twelve months.
#
# Six, not twelve, because `lead_score` already treats an award as fully fresh only
# through six months, and because the rep who phoned a district ten months after
# obligation was told the competitor's install was already finishing (Kerry,
# 2026-09-01). ONE constant, read by the rich card policy, the fallback daily card,
# the daily list and the follow-up nudges, so no surface can drift older than the
# others. A lead past this ceiling is still GOLD, still searchable, still exportable —
# it is simply never pushed unasked.
CARD_MAX_AWARD_MONTHS = 6


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
        # Missing dollars/window cannot prove money is available; keep as WATCH.
        if item.amount is None or item.amount <= 0:
            return Lead(item, LeadGrade.WATCH)
        if end is None or end < today:
            return Lead(item, LeadGrade.WATCH)
        # Gold-fresh / silver-older split (Chase): a security award obligated within the
        # last FRESH_MONTHS is a hot new buyer (GOLD); the same award obligated over a
        # year ago still has an open window but the awardee likely has vendors locked in,
        # so it drops to SILVER.
        #
        # An UNKNOWN award date is SILVER, not GOLD (Chase, 2026-07-22). This inverts the
        # earlier rule, which kept undated awards GOLD "rather than guess the money is
        # stale". That reasoning was backwards: GOLD is defined as "just got funding …
        # ideally < 12 months old", so awarding it on the ABSENCE of a date grades on
        # absent evidence and asserts a recency the data cannot support (Constitution
        # rule 1). It is also not hypothetical — the 347 `ca-grants-award` rows carry no
        # award date at all (ca_grants.py sets event_date=""), so this rule governs the
        # majority of the award pool. They remain fully searchable and exportable as
        # SILVER; they are simply not served as proactive GOLD.
        awarded = _parse_date(item.event_date)
        if awarded is None:
            return Lead(item, LeadGrade.SILVER)
        if awarded < today - timedelta(days=FRESH_MONTHS * 30):
            return Lead(item, LeadGrade.SILVER)
        return Lead(item, LeadGrade.GOLD)

    # RFPs are SILVER at best, never GOLD/PLATINUM (Chase, 2026-07-19): winning an RFP is
    # a lot of work with a relatively low hit rate, so an open solicitation is a Silver
    # lead — it never outranks a grant a district has already been awarded. An OPEN RFP
    # (future deadline) is SILVER; once the deadline passes it drops to WATCH. This covers
    # the aggregator (source=='rfp') and the state bid sources (WEBS/SAM/OregonBuys) alike.
    if item.source == "rfp" or item.source in RFP_SOURCES:
        deadline = _parse_date(item.end)
        return Lead(
            item,
            LeadGrade.SILVER if deadline and deadline >= today else LeadGrade.WATCH,
        )

    # grants.gov + anything unrecognized: keep as watch, never drop (CLAUDE.md).
    return Lead(item, LeadGrade.WATCH)


def card_award_cutoff(today: date) -> date:
    """The oldest obligation date Grant will still push unasked, inclusive.

    Calendar months, day-clamped: from 2026-08-31 six months back is 2026-02-28, not a
    ValueError and not a silent 183-day window. Kept separate from `is_fresh` (the
    grade's 360-day rule) because the two answer different questions.
    """
    month_index = today.year * 12 + (today.month - 1) - CARD_MAX_AWARD_MONTHS
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def award_is_card_fresh(occurred_on: object, today: date) -> bool:
    """Whether an award date is recent enough for Grant to surface it proactively.

    Undated is NOT fresh: a card's whole claim is recency, and `grade` already sends
    undated awards to SILVER for the same reason. A FUTURE date is not fresh either —
    it is bad data, and `award_age_phrase` refuses to describe it. Both fail closed:
    the lead stays in the pool for a human to find, and is simply never pushed.
    """
    occurred = _parse_date(str(occurred_on or "")[:10])
    return occurred is not None and card_award_cutoff(today) <= occurred <= today


def is_fresh(item: RawItem, today: date | None = None) -> bool:
    """Return whether the source explicitly dates the event within FRESH_MONTHS.

    Spend-window start is not an award announcement date and is never substituted.
    """
    today = today or date.today()
    occurred = _parse_date(item.event_date)
    return occurred is not None and occurred >= today - timedelta(
        days=FRESH_MONTHS * 30
    )


# ---------------------------------------------------------------- quality gate (rank)

# How addressable each program's dollars are with Monarch's catalog (cameras, access
# control, door hardening). Chase's rule: reps must trust every proactive alert, so
# software-heavy programs rank low even at high dollar amounts.
PROGRAM_FIT: dict[str, float] = {
    "SVPP": 1.0,  # school physical security — the bullseye
    "CSSGP": 1.0,  # MI: eligible costs are literally the catalog
    "PCCD": 1.0,  # PA school safety
    "NSGP": 0.9,  # nonprofit hardening — near-pure physical security
    "STOP": 0.5,  # skews software/threat-assessment (docs/FINDINGS.md)
    "RFP:SECURITY": 1.0,  # an open camera/access-control RFP is a direct buy signal
}
_DEFAULT_FIT = 0.6  # RFPs and unknown programs: relevant but unproven
_AMOUNT_NORM = 500_000  # SVPP max award — a natural "full marks" dollar anchor


def lead_score(
    program: str, amount: float | None, event_date: str, today: date | None = None
) -> float:
    """0..1 rank using the explicit event date, dollars, and program camera-fit.

    Freshness dominates by design (Chase: 'freshness is everything') — a $500K award
    from 3 years ago ranks below a $100K award from last month. A spend-window start
    must never be passed as ``event_date``; unknown occurrence dates remain conservative.
    """
    today = today or date.today()
    occurred = _parse_date(event_date)
    if occurred is None:
        fresh = 0.3  # unknown event date: visible, never above known-fresh events
    else:
        age_months = max(0.0, (today - occurred).days / 30)
        # 1.0 through 6 months, linear decay to 0.15 by 36 months
        fresh = (
            1.0 if age_months <= 6 else max(0.15, 1.0 - (age_months - 6) / 30 * 0.85)
        )
    dollars = min((amount or 0) / _AMOUNT_NORM, 1.0) if amount and amount > 0 else 0.3
    fit = PROGRAM_FIT.get((program or "").upper(), _DEFAULT_FIT)
    return round(fresh * (0.5 + 0.5 * dollars) * fit, 4)


def feedback_multiplier(points: list[int], minimum_sample: int = 10) -> float:
    """Return a cautious 0.85..1.15 reward adjustment after enough human outcomes.

    Before ``minimum_sample`` outcomes, the multiplier is neutral. This keeps a few
    reactions or one bad-lead click from destabilizing the quality rank.
    """
    if len(points) < minimum_sample:
        return 1.0
    average = sum(points) / len(points)
    return round(max(0.85, min(1.15, 1.0 + (average / 8.0) * 0.15)), 4)
