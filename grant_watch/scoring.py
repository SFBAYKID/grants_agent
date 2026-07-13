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


# ---------------------------------------------------------------- quality gate (rank)

# How addressable each program's dollars are with Monarch's catalog (cameras, access
# control, door hardening). Chase's rule: reps must trust every digest line, so
# software-heavy programs rank low even at high dollar amounts.
PROGRAM_FIT: dict[str, float] = {
    "SVPP": 1.0,    # school physical security — the bullseye
    "CSSGP": 1.0,   # MI: eligible costs are literally the catalog
    "PCCD": 1.0,    # PA school safety
    "NSGP": 0.9,    # nonprofit hardening — near-pure physical security
    "STOP": 0.5,    # skews software/threat-assessment (docs/FINDINGS.md)
}
_DEFAULT_FIT = 0.6      # RFPs and unknown programs: relevant but unproven
_AMOUNT_NORM = 500_000  # SVPP max award — a natural "full marks" dollar anchor


def lead_score(program: str, amount: float | None, start: str,
               today: date | None = None) -> float:
    """0..1 rank for the digest: freshness x dollars x program camera-fit.

    Freshness dominates by design (Chase: 'freshness is everything') — a $500K award
    from 3 years ago ranks below a $100K award from last month.
    """
    today = today or date.today()
    start_d = _parse_date(start)
    if start_d is None:
        fresh = 0.3  # unknown start: keep it visible but never above known-fresh leads
    else:
        age_months = max(0.0, (today - start_d).days / 30)
        # 1.0 through 6 months, linear decay to 0.15 by 36 months
        fresh = 1.0 if age_months <= 6 else max(0.15, 1.0 - (age_months - 6) / 30 * 0.85)
    dollars = min((amount or 0) / _AMOUNT_NORM, 1.0) if amount and amount > 0 else 0.3
    fit = PROGRAM_FIT.get((program or "").upper(), _DEFAULT_FIT)
    return round(fresh * (0.5 + 0.5 * dollars) * fit, 4)
