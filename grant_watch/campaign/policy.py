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
from urllib.parse import parse_qs, urlsplit

import tldextract

# Offline registrable-domain extractor: uses the Public Suffix List snapshot bundled in
# the tldextract package (``suffix_list_urls=()`` => never the network — gov servers are
# not hammered and tests stay deterministic). One shared instance.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _host(url: str) -> str:
    """Normalized hostname for same-site comparison; '' when unparseable."""
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.").rstrip(".")
    except ValueError:
        return ""


def _registrable(host: str) -> str:
    """Return the registrable domain (public suffix + one label) via the PSL, or '' when
    the host IS a bare public suffix / has no registrable part.

    Proper eTLD+1, not a hand-rolled guess: ``sd.vallelindo.k12.ca.us`` ->
    ``vallelindo.k12.ca.us``; ``k12.ca.us`` and ``net`` -> '' (they are public suffixes)."""
    parts = _EXTRACT((host or "").strip().lower())
    return f"{parts.domain}.{parts.suffix}" if parts.domain and parts.suffix else ""


def _same_site(left: str, right: str) -> bool:
    """Two hosts belong to the same organization iff they share ONE registrable domain
    (eTLD+1) under the Public Suffix List.

    This closes the cross-district hole a suffix test cannot: a bare public suffix
    (``k12.ca.us``, ``net``) has no registrable domain and never binds, and two districts
    under the same suffix (``montebello.k12.ca.us`` vs ``valle.k12.ca.us``) have DIFFERENT
    registrable domains and never cross-bind. Exact host and true subdomains share it.
    NOTE: a private shared-hosting domain NOT in the PSL (e.g. ``txed.net``) collapses its
    subdomains to one registrable domain; within a single lead the email and website hosts
    are compared and are typically identical, and such a lead can never be draft-ready
    (its website provenance is heuristic, not exact) — so no auto-draft rests on it."""
    left_domain = _registrable(left)
    right_domain = _registrable(right)
    return bool(left_domain and left_domain == right_domain)


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
CRM_FRESH_HOURS = 24  # reuse the deployed CRM-snapshot freshness window

# Amounts an obligated award figure may NEVER be labelled as.
FORBIDDEN_AMOUNT_WORDS = ("remaining", "available", "left to spend")


def forbidden_amount_label(text: str) -> str:
    """Return the first forbidden word labelling the money line, or "" if clean.

    An obligated figure is what was AWARDED. Calling it "remaining" or "available"
    claims a balance nobody measured — the district may have spent every dollar — and
    a rep quoting that number back to a customer is repeating a figure we invented.
    Only the line carrying the amount is inspected: "available" is perfectly honest
    elsewhere on a card (an open spend window, a contact being reachable), and a
    whole-card scan would fail closed on true sentences.
    """
    for line in text.splitlines():
        if "$" not in line:
            continue
        lowered = line.lower()
        for word in FORBIDDEN_AMOUNT_WORDS:
            if word in lowered:
                return word
    return ""


# Award event types that can back a rich card. Grade is priority; the EVENT says what
# happened (record_semantics), so eligibility keys off the event, never the grade.
AWARD_EVENT_TYPES = ("award_announced", "award_obligated")

# Organization kinds the card supports. `city` is kept in the model but its qualification
# is DEFERRED (Chase A3) until a non-heuristic runtime city-kind source exists.
SUPPORTED_ENTITY_KINDS = ("school", "school_district", "city")
QUALIFYING_ENTITY_KINDS = ("school", "school_district")  # v1 scope
# Provenance that may establish an entity kind. Name heuristics alone NEVER qualify.
VALID_KIND_PROVENANCE = ("source", "nces", "census", "reviewed")

# Salesforce lookup states that back a DRAFT-READY card (fresh AND complete): the rep
# may ask Persequor to draft immediately. Ambiguous is handled separately as a
# research-needed card; partial/unavailable/stale/missing are ineligible.
SAFE_CRM_STATES = ("exact_match", "complete_no_match")
# Fresh CRM states that back a RESEARCH-NEEDED card: eligible to post, but it must claim
# no relationship and no net-new, route by territory only, and offer NO draft action
# until a human resolves the match (Chase, 2026-07-23).
RESEARCH_CRM_STATES = ("ambiguous",)

# Human-reviewed authoritative government/education directory hosts. A contact email or
# official website may be TRUSTED from an EXACT, id-bound record on one of these hosts,
# but such a host is NEVER treated as an organization's own website (it lists many
# organizations). Allowlist, not a suffix heuristic: an unlisted host is untrusted until
# a human reviews and adds it.
REVIEWED_DIRECTORY_HOSTS = frozenset(
    {
        "nces.ed.gov",  # federal NCES school/district locator (exact-bindable by NCES id)
        "cde.ca.gov",  # California Dept. of Education school directory
    }
)

# Contact types the card may show.
CONTACT_TYPES = ("named_direct", "official_general")


class WebsiteProvenance(str, Enum):
    """Typed, non-heuristic reason an official website was trusted (frozen on the card).

    A name match NEVER qualifies for draft-ready ownership. ``nces`` is populated by
    the exact-ID district-detail binder; the generic ``authoritative_directory``
    variant remains reserved for a future reviewed directory writer."""

    NCES = "nces"
    AUTHORITATIVE_DIRECTORY = "authoritative_directory"
    VERIFIED_ORG_PAGE = "verified_org_page"
    NONE = "none"


class ContactBinding(str, Enum):
    """Typed reason a contact email was bound to the ORGANIZATION (frozen on the card).

    ``org_site`` = the email domain matches the verified organization website;
    ``authoritative_directory`` = the email appears verbatim in an exact, id-bound
    record on a reviewed directory host. The scrape page alone never binds (critic C1)."""

    ORG_SITE = "org_site"
    AUTHORITATIVE_DIRECTORY = "authoritative_directory"
    NONE = "none"


class CardMode(str, Enum):
    """Whether an eligible card may draft immediately or needs human review first."""

    DRAFT_READY = "draft_ready"
    RESEARCH_NEEDED = "research_needed"


# Website-provenance kinds that PROVE the organization owns the website by an exact,
# authoritative record — the only kinds that may back a DRAFT-READY card (Chase,
# 2026-07-23). VERIFIED_ORG_PAGE rests on ``finder._looks_official`` (a name-token
# anchor), so it is NOT proven ownership and caps a card at research-needed (no
# auto-draft). The NCES binder now feeds the NCES kind from an exact district-id page;
# the general authoritative-directory kind has no writer yet.
EXACT_WEBSITE_PROVENANCE = frozenset(
    {WebsiteProvenance.NCES, WebsiteProvenance.AUTHORITATIVE_DIRECTORY}
)
_EXACT_WEBSITE_PROVENANCE_VALUES = frozenset(p.value for p in EXACT_WEBSITE_PROVENANCE)


def is_website_ownership_proven(provenance: object) -> bool:
    """True iff the website's organization ownership is an EXACT authoritative record, not
    a name heuristic. Accepts a ``WebsiteProvenance`` or its ``.value`` (the frozen
    snapshot stores the value string)."""
    value = (
        provenance.value if isinstance(provenance, WebsiteProvenance) else provenance
    )
    return value in _EXACT_WEBSITE_PROVENANCE_VALUES


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
    CONTACT_DOMAIN = "contact_email_not_bound_to_organization"
    CRM_UNSAFE = "crm_partial_unavailable_or_stale"


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
    nces_id: str  # exact federal id; enables authoritative-directory binding
    state_verified: bool
    award_url_safe: bool
    official_website: str
    # Raw inputs for the TYPED website-provenance decision (never a name heuristic):
    org_profile_found: bool  # a verbatim org-site scrape succeeded
    org_profile_evidence_url: str  # the page that scrape verified
    nces_website: str  # exact-ID NCES-published official site; '' until bound
    contact_status: str  # 'verified' | ... ; only 'verified' qualifies
    contact_type: str
    contact_email: str
    contact_evidence_url: str  # the page the email was verified on (host + exact-id)
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
    """Result of judging one candidate, plus the typed provenance to freeze on the card."""

    eligible: bool
    reason: Reason
    tier: str  # 'gold' | 'platinum' | ''
    website_provenance: WebsiteProvenance = WebsiteProvenance.NONE
    contact_binding: ContactBinding = ContactBinding.NONE
    card_mode: CardMode = CardMode.DRAFT_READY


def website_provenance(
    website: str,
    *,
    org_profile_found: bool,
    org_profile_evidence_url: str,
    contact_status: str,
    contact_evidence_url: str,
    nces_website: str,
) -> WebsiteProvenance:
    """Typed reason to trust an official website. The POLICY layer never re-guesses from a
    name — but honesty note (critic H2, 2026-07-23): the tie between ``website`` and the
    specific awardee still rests on the enrichment anchor for
    ``WebsiteProvenance.VERIFIED_ORG_PAGE``. That weaker candidate can support a
    research card but never a draft-ready action. ``WebsiteProvenance.NCES`` is the
    independent exact-ID path and is populated only by the official NCES binder.

    A reviewed-directory host is NEVER an organization's own site. Accept only: the exact
    NCES-published site, or a page verified on the org's OWN domain — an org-profile
    scrape, or a verbatim-verified contact evidence page on that same domain (the
    latter rests on the SAME name anchor, not a stronger one)."""
    site = _host(website)
    if not site or site in REVIEWED_DIRECTORY_HOSTS:
        return WebsiteProvenance.NONE
    if nces_website and _same_site(site, _host(nces_website)):
        return WebsiteProvenance.NCES
    if org_profile_found and _same_site(site, _host(org_profile_evidence_url)):
        return WebsiteProvenance.VERIFIED_ORG_PAGE
    contact_host = _host(contact_evidence_url)
    if (
        contact_status == "verified"
        and contact_host
        and contact_host not in REVIEWED_DIRECTORY_HOSTS
        and _same_site(site, contact_host)
    ):
        return WebsiteProvenance.VERIFIED_ORG_PAGE
    return WebsiteProvenance.NONE


def _authoritative_exact(contact_evidence_url: str, nces_id: str) -> bool:
    """True only for an exact, id-bound record on a reviewed authoritative directory.

    Exact ORGANIZATION binding, not a name match and NOT a substring: the evidence URL
    must be on a reviewed directory host AND carry this lead's exact authoritative id as a
    whole query value or path segment. A substring match (``in``) would let one district's
    id ``062271`` bind another's record ``?ID=0622710`` — the wrong organization. Only the
    NCES id is captured today, so an ``nces.ed.gov`` record can bind; a state-directory
    record (e.g. a CA CDS code we do not store) cannot yet and stays closed — fail-closed."""
    host = _host(contact_evidence_url)
    identifier = nces_id.strip()
    if not host or host not in REVIEWED_DIRECTORY_HOSTS or not identifier:
        return False
    if host != "nces.ed.gov":
        return False
    parsed = urlsplit(contact_evidence_url)
    tokens: set[str] = set()
    for values in parse_qs(parsed.query).values():
        tokens.update(values)
    tokens.update(segment for segment in parsed.path.split("/") if segment)
    return identifier in tokens


def contact_binding(
    email: str,
    website: str,
    *,
    contact_evidence_url: str,
    nces_id: str,
) -> ContactBinding:
    """Bind a non-personal contact email to the ORGANIZATION, not the scrape page.

    Personal-mailbox rejection is applied by the caller BEFORE this. An email binds when
    its domain matches the verified organization website (org-site), or when it appears
    verbatim in an exact, id-bound record on a reviewed authoritative directory. The page
    an email was merely scraped from never binds it (critic C1 / Chase 2026-07-23)."""
    email_host = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if email_host and _same_site(email_host, _host(website)):
        return ContactBinding.ORG_SITE
    if _authoritative_exact(contact_evidence_url, nces_id):
        return ContactBinding.AUTHORITATIVE_DIRECTORY
    return ContactBinding.NONE


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

    # --- links + website (typed provenance; never a name heuristic) ---------------
    if not c.award_url_safe:
        return Eligibility(False, Reason.AWARD_URL_UNSAFE, "")
    if not c.official_website:
        return Eligibility(False, Reason.NO_WEBSITE, "")
    provenance = website_provenance(
        c.official_website,
        org_profile_found=c.org_profile_found,
        org_profile_evidence_url=c.org_profile_evidence_url,
        contact_status=c.contact_status,
        contact_evidence_url=c.contact_evidence_url,
        nces_website=c.nces_website,
    )
    if provenance is WebsiteProvenance.NONE:
        return Eligibility(False, Reason.WEBSITE_UNVERIFIED, "")

    # --- contact evidence (bound to the ORGANIZATION, not the scrape page) ---------
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
    binding = contact_binding(
        c.contact_email,
        c.official_website,
        contact_evidence_url=c.contact_evidence_url,
        nces_id=c.nces_id,
    )
    if binding is ContactBinding.NONE:
        return Eligibility(False, Reason.CONTACT_DOMAIN, "")

    # --- Salesforce freshness gate ------------------------------------------------
    crm_checked = _parse_datetime(c.crm_checked_at)
    crm_fresh = (
        crm_checked is not None
        and crm_checked <= policy_now
        and (policy_now - crm_checked).total_seconds() <= CRM_FRESH_HOURS * 3600
    )
    crm_draft_safe = c.crm_state in SAFE_CRM_STATES and crm_fresh
    crm_research = c.crm_state in RESEARCH_CRM_STATES and crm_fresh
    if not (crm_draft_safe or crm_research):
        # partial, unavailable, missing, or any STALE lookup — ineligible for v1.
        return Eligibility(False, Reason.CRM_UNSAFE, "")

    # --- card mode: DRAFT-READY requires BOTH a draft-safe CRM AND PROVEN website ---
    # ownership (an exact authoritative record). A heuristic-owned website (a name-token
    # anchor) or an ambiguous CRM caps the card at research-needed — safe, no auto-draft
    # (Chase, 2026-07-23). No relationship/net-new claim and no CRM-owner routing on the
    # ambiguous path; both are enforced downstream in preparation and card.
    website_proven = provenance in EXACT_WEBSITE_PROVENANCE
    mode = (
        CardMode.DRAFT_READY
        if crm_draft_safe and website_proven
        else CardMode.RESEARCH_NEEDED
    )

    # --- eligible: gold, or platinum if a very fresh physical-security award -------
    days_since_award = (c.today - awarded).days
    tier = (
        "platinum" if days_since_award <= PLATINUM_DAYS and c.program_fit_ok else "gold"
    )
    return Eligibility(True, Reason.ELIGIBLE, tier, provenance, binding, mode)
