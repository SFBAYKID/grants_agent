"""Lead grading: GOLD / SILVER / WATCH, per Chase's definitions in CLAUDE.md.

Rules (v1 — deliberately simple, tuned as feedback arrives through Grant's threads):
  GOLD    an actual award (money in hand) whose spend window is still open and whose
          amount is positive. Freshness matters: awards started within the last
          FRESH_MONTHS are the hottest; older-but-open awards stay gold but rank
          below fresh events — expired windows drop to watch.
  SILVER  an open RFP/bid matched to our keywords (WEBS, SAM.gov).
  WATCH   everything ambiguous: grants.gov opportunities (pipeline signal, not money),
          negative/zero amounts (de-obligations — found in the 2026-07-13 live run),
          unknown windows. Per CLAUDE.md we keep these rather than drop them.
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import Lead, LeadGrade, RawItem

# Sources whose items are awards (money granted) vs solicitations (RFPs) vs signals.
AWARD_SOURCES_PREFIX = (
    "usaspending:",
    "usaspending-subaward:",
    "ca-grants-award:",
    "seed:",
)
RFP_SOURCES = ("webs", "sam.gov", "oregonbuys", "rfp")
SIGNAL_SOURCES = ("grants.gov",)

FRESH_MONTHS = 12  # Chase: after ~a year, awardees likely have vendors locked in.
FRESH_RFP_DAYS = 30  # Chase: an RFP "just put out" within ~a month is GOLD, else SILVER.


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
        return Lead(item, LeadGrade.GOLD)

    # Security-RFP discovery: an OPEN RFP is GOLD when freshly posted (an active buyer
    # who just put it out), SILVER when open but older, WATCH once the deadline passes
    # (Chase, 2026-07-18). The posting date must be verified (item.event_date); an
    # unproven posting date defaults to SILVER, never GOLD on a guess.
    if item.source == "rfp":
        deadline = _parse_date(item.end)
        if deadline is None or deadline < today:
            return Lead(item, LeadGrade.WATCH)
        posted = _parse_date(item.event_date)
        fresh = posted is not None and posted >= today - timedelta(days=FRESH_RFP_DAYS)
        return Lead(item, LeadGrade.GOLD if fresh else LeadGrade.SILVER)

    if item.source in RFP_SOURCES:
        deadline = _parse_date(item.end)
        return Lead(
            item,
            LeadGrade.SILVER if deadline and deadline >= today else LeadGrade.WATCH,
        )

    # grants.gov + anything unrecognized: keep as watch, never drop (CLAUDE.md).
    return Lead(item, LeadGrade.WATCH)


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
