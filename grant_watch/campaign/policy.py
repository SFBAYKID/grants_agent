"""Rich award-card eligibility policy — pure, typed, testable, no I/O.

The single place that decides whether a candidate may become a proactive rich card. It
takes a fully-gathered `CandidateEvidence` snapshot of inputs (the preparation worker
does the I/O and gathering; this module only judges) and returns an `Eligibility` with a
machine-usable rejection `reason` so the shadow report can count rejections by cause.

Every rule here is honesty-critical: a card that passes reaches a real rep with a real
award/contact/owner claim. When in doubt the answer is INELIGIBLE — never a softened
"maybe". If NO candidate qualifies, the campaign posts nothing; it never falls back to an
RFP, a bulletin, a stale contact, a generic page, a fabricated org kind, or an incomplete
CRM result (that fallback discipline lives in the selector, but the predicate here is what
makes "nothing qualified" honest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from math import isfinite

# Bump when a rule changes. Stored on the snapshot as PROVENANCE ONLY — never part of a
# delivery-uniqueness key (that would re-post the eligible backlog on every tightening;
# critic C2).
POLICY_VERSION = 1

# Freshness constants (named, per spec). Calendar days.
AWARD_MAX_MONTHS = 12  # award no older than this
PLATINUM_DAYS = 7  # a verified award within a week is the platinum presentation tier
# The observation must be re-confirmed by a COMPLETE, SUCCESSFUL run within this window
# (Chase A1). 6 calendar days, not 4, so a Friday award survives a weekend + one holiday
# on a weekday-only poll (critic M1).
OBSERVATION_FRESH_DAYS = 6
CONTACT_FRESH_DAYS = 30  # a verified public contact re-checked within this window
ACTIVITY_FRESH_DAYS = (
    30  # a completed Salesforce call within this window may route/label
)
CRM_FRESH_HOURS = 24  # reuse the deployed CRM-snapshot freshness window

# Amounts an obligated award figure may NEVER be labelled as.
FORBIDDEN_AMOUNT_WORDS = ("remaining", "available", "left to spend")

# Award event types that can back a rich card. Grade is priority; the EVENT says what
# happened (record_semantics), so eligibility keys off the event, never the grade.
AWARD_EVENT_TYPES = ("award_announced", "award_obligated")

# Organization kinds the card supports. `city` is kept in the model but its qualification
# is DEFERRED (Chase A3) until a non-heuristic runtime city-kind source exists.
SUPPORTED_ENTITY_KINDS = ("school", "school_district", "city")
QUALIFYING_ENTITY_KINDS = ("school", "school_district")  # v1 scope
# Provenance that may establish an entity kind. Name heuristics alone NEVER qualify.
VALID_KIND_PROVENANCE = ("source", "nces", "census", "reviewed")

# Salesforce lookup states that are safe (fresh AND complete). Ambiguous/partial/
# unavailable/stale are NOT here and make the card temporarily ineligible.
SAFE_CRM_STATES = ("exact_match", "complete_no_match")

# Contact types the card may show.
CONTACT_TYPES = ("named_direct", "official_general")

# Personal / private mailbox providers rejected for this campaign even when the address
# appears on an official page (spec E).
PERSONAL_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "zoho.com",
        "comcast.net",
        "att.net",
        "verizon.net",
        "sbcglobal.net",
        "cox.net",
    }
)


class Reason(str, Enum):
    """Machine-usable rejection cause (also the shadow report's rejection buckets)."""

    ELIGIBLE = "eligible"
    NOT_GOLD = "not_gold_grade"
    NOT_AWARD_EVENT = "not_verified_award_event"
    UNVERIFIED_EVENT = "award_event_not_verified"
    EVENT_EVIDENCE_MISSING = "award_event_evidence_missing"
    BAD_AMOUNT = "amount_not_finite_positive"
    AWARD_DATE_MISSING = "award_date_missing_or_imprecise"
    AWARD_DATE_FUTURE = "award_date_in_future"
    AWARD_TOO_OLD = "award_older_than_12_months"
    WINDOW_CLOSED = "spend_window_missing_or_closed"
    STALE_OBSERVATION = "observation_not_confirmed_by_recent_complete_run"
    KIND_UNSUPPORTED = "entity_kind_unsupported"
    KIND_DEFERRED = "entity_kind_city_deferred"
    KIND_PROVENANCE = "entity_kind_provenance_heuristic_or_missing"
    STATE_PROVENANCE = "state_provenance_unverified"
    AWARD_URL_UNSAFE = "award_url_missing_or_unsafe"
    NO_WEBSITE = "official_website_missing"
    WEBSITE_UNVERIFIED = "official_website_provenance_missing"
    CONTACT_MISSING = "contact_missing_or_unverified"
    CONTACT_STALE = "contact_evidence_stale"
    CONTACT_URL_UNSAFE = "contact_evidence_url_missing_or_unsafe"
    CONTACT_PERSONAL = "contact_personal_mailbox"
    CONTACT_DOMAIN = "contact_email_domain_mismatch"
    CRM_UNSAFE = "crm_ambiguous_partial_unavailable_or_stale"


@dataclass(frozen=True)
class CandidateEvidence:
    """Everything the predicate needs, gathered by the preparation worker (no I/O here).

    Kept a plain value object so the policy is a pure function of its inputs and is fully
    testable with hand-built fixtures — which the award path REQUIRES, because the local
    database has zero verified award events (all `record_observed`).
    """

    lead_id: int
    lead_grade: str
    event_type: str
    event_verified: bool
    event_evidence_complete: bool
    amount: float | None
    award_date: str  # ISO; "" when absent
    award_date_precision: str  # 'day' | 'month' | 'unknown'
    spend_window_start: str  # ISO; "" when absent
    spend_window_end: str  # ISO; "" when absent
    last_confirmed_at: str  # ISO from a COMPLETE run; "" when never confirmed
    last_confirmed_run_complete: bool
    entity_kind: str  # 'school' | 'school_district' | 'city' | ''
    entity_kind_provenance: str  # one of VALID_KIND_PROVENANCE, or ''
    state_verified: bool
    award_url_safe: bool
    official_website: str
    official_website_verified: bool
    contact_status: str  # 'verified' | ... ; only 'verified' qualifies
    contact_type: str
    contact_email: str
    contact_official_domain: str
    contact_last_verified_at: str  # ISO
    contact_expires_at: str  # ISO; explicit lifecycle expiry
    contact_evidence_url_safe: bool
    program_fit_ok: bool  # strong physical-security program (for platinum)
    crm_state: str  # 'exact_match' | 'complete_no_match' | 'ambiguous' | ...
    crm_checked_at: str  # ISO
    # a marker set by the caller for tests; real callers pass date.today()
    today: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    now_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class Eligibility:
    """Result of judging one candidate."""

    eligible: bool
    reason: Reason
    tier: str  # 'gold' | 'platinum' | ''


def _parse_date(iso: str) -> date | None:
    """Lenient ISO date parse (sources emit '', dates, or full timestamps)."""
    if not iso:
        return None
    try:
        return date.fromisoformat(iso[:10])
    except ValueError:
        return None


def _days_since(iso: str, today: date) -> int | None:
    """Whole days from an ISO date to `today`, or None if unparseable."""
    d = _parse_date(iso)
    return (today - d).days if d is not None else None


def _parse_datetime(iso: str) -> datetime | None:
    """Parse one ISO timestamp as UTC; a bare date begins at midnight UTC."""
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(iso[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _twelve_month_cutoff(today: date) -> date:
    """Return the same calendar date one year ago, handling leap day explicitly."""
    years = AWARD_MAX_MONTHS // 12
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def evaluate(c: CandidateEvidence) -> Eligibility:
    """Judge one candidate. Returns the FIRST failing reason (order = cheapest/most
    fundamental first) so the shadow report attributes a single clear cause per reject."""

    # --- award truth -------------------------------------------------------------
    if c.lead_grade != "gold":
        return Eligibility(False, Reason.NOT_GOLD, "")
    if c.event_type not in AWARD_EVENT_TYPES:
        return Eligibility(False, Reason.NOT_AWARD_EVENT, "")
    if not c.event_verified:
        return Eligibility(False, Reason.UNVERIFIED_EVENT, "")
    if not c.event_evidence_complete:
        return Eligibility(False, Reason.EVENT_EVIDENCE_MISSING, "")
    if c.amount is None or not isfinite(c.amount) or not (c.amount > 0):
        return Eligibility(False, Reason.BAD_AMOUNT, "")

    awarded = _parse_date(c.award_date)
    if awarded is None or c.award_date_precision not in ("day", "month"):
        return Eligibility(False, Reason.AWARD_DATE_MISSING, "")
    if awarded > c.today:
        return Eligibility(False, Reason.AWARD_DATE_FUTURE, "")
    if awarded < _twelve_month_cutoff(c.today):
        return Eligibility(False, Reason.AWARD_TOO_OLD, "")

    window_start = _parse_date(c.spend_window_start)
    window_end = _parse_date(c.spend_window_end)
    if (
        window_start is None
        or window_end is None
        or not (window_start <= c.today <= window_end)
    ):
        return Eligibility(False, Reason.WINDOW_CLOSED, "")

    # --- freshness: re-confirmed by a COMPLETE successful run (A1) ----------------
    confirmed_days = _days_since(c.last_confirmed_at, c.today)
    if (
        not c.last_confirmed_run_complete
        or confirmed_days is None
        or confirmed_days < 0
        or confirmed_days > OBSERVATION_FRESH_DAYS
    ):
        return Eligibility(False, Reason.STALE_OBSERVATION, "")

    # --- organization kind + state provenance ------------------------------------
    if c.entity_kind not in SUPPORTED_ENTITY_KINDS:
        return Eligibility(False, Reason.KIND_UNSUPPORTED, "")
    if c.entity_kind not in QUALIFYING_ENTITY_KINDS:
        # `city` is modelled but deferred until non-heuristic provenance exists (A3).
        return Eligibility(False, Reason.KIND_DEFERRED, "")
    if c.entity_kind_provenance not in VALID_KIND_PROVENANCE:
        return Eligibility(False, Reason.KIND_PROVENANCE, "")
    if not c.state_verified:
        return Eligibility(False, Reason.STATE_PROVENANCE, "")

    # --- links + website ---------------------------------------------------------
    if not c.award_url_safe:
        return Eligibility(False, Reason.AWARD_URL_UNSAFE, "")
    if not c.official_website:
        return Eligibility(False, Reason.NO_WEBSITE, "")
    if not c.official_website_verified:
        return Eligibility(False, Reason.WEBSITE_UNVERIFIED, "")

    # --- contact evidence --------------------------------------------------------
    if c.contact_status != "verified" or c.contact_type not in CONTACT_TYPES:
        return Eligibility(False, Reason.CONTACT_MISSING, "")
    contact_days = _days_since(c.contact_last_verified_at, c.today)
    contact_expiry = _parse_datetime(c.contact_expires_at)
    policy_now = c.now_utc.astimezone(timezone.utc)
    if (
        contact_days is None
        or contact_days < 0
        or contact_days > CONTACT_FRESH_DAYS
        or contact_expiry is None
        or contact_expiry <= policy_now
    ):
        return Eligibility(False, Reason.CONTACT_STALE, "")
    if not c.contact_evidence_url_safe:
        return Eligibility(False, Reason.CONTACT_URL_UNSAFE, "")
    email_domain = (
        c.contact_email.rsplit("@", 1)[-1].lower() if "@" in c.contact_email else ""
    )
    if email_domain in PERSONAL_EMAIL_DOMAINS:
        return Eligibility(False, Reason.CONTACT_PERSONAL, "")
    if not email_domain or email_domain != c.contact_official_domain.lower():
        return Eligibility(False, Reason.CONTACT_DOMAIN, "")

    # --- Salesforce: fresh AND complete ------------------------------------------
    crm_checked = _parse_datetime(c.crm_checked_at)
    if (
        c.crm_state not in SAFE_CRM_STATES
        or crm_checked is None
        or crm_checked > policy_now
        or (policy_now - crm_checked).total_seconds() > CRM_FRESH_HOURS * 3600
    ):
        return Eligibility(False, Reason.CRM_UNSAFE, "")

    # --- eligible: gold, or platinum if a very fresh physical-security award ------
    days_since_award = (c.today - awarded).days
    tier = (
        "platinum" if days_since_award <= PLATINUM_DAYS and c.program_fit_ok else "gold"
    )
    return Eligibility(True, Reason.ELIGIBLE, tier)
