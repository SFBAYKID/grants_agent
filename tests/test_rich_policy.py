"""Rich award-card eligibility policy — happy paths and every rejection reason.

Award-path fixtures are HAND-BUILT: the local database has zero verified award events
(all record_observed), so nothing here is "verified against seed data" — the policy is a
pure function judged against constructed evidence, exactly as the design requires.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from grant_watch.campaign import policy
from grant_watch.campaign.policy import (
    CandidateEvidence,
    CardMode,
    ContactBinding,
    Reason,
    WebsiteProvenance,
)

TODAY = date(2026, 7, 22)


def _valid(**over: object) -> CandidateEvidence:
    """A fully-eligible GOLD school-district candidate; override one field per test."""
    base = CandidateEvidence(
        lead_id=1,
        lead_grade="gold",
        event_type="award_obligated",
        event_verified=True,
        event_evidence_complete=True,
        amount=500_000.0,
        award_date="2026-06-01",
        award_date_precision="day",
        spend_window_start="2025-10-10",
        spend_window_end="2028-09-30",
        last_confirmed_at="2026-07-20",
        last_confirmed_run_complete=True,
        entity_kind="school_district",
        entity_kind_provenance="nces",
        nces_id="0622710",
        state_verified=True,
        award_url_safe=True,
        official_website="https://montebelloschools.net",
        org_profile_found=True,
        org_profile_evidence_url="https://montebelloschools.net/contact",
        nces_website="",
        contact_status="verified",
        contact_type="named_direct",
        contact_email="jdoe@montebelloschools.net",
        contact_evidence_url="https://montebelloschools.net/staff",
        contact_last_verified_at="2026-07-10",
        contact_expires_at="2026-08-10T00:00:00+00:00",
        contact_evidence_url_safe=True,
        program_fit_ok=True,
        crm_state="exact_match",
        crm_checked_at="2026-07-22T12:00:00+00:00",
        today=TODAY,
        now_utc=datetime(2026, 7, 22, 12, tzinfo=timezone.utc),
    )
    return replace(base, **over)  # type: ignore[arg-type]


def test_valid_gold_school_district_is_eligible() -> None:
    """Provide test-local behavior for valid gold school district is eligible."""
    result = policy.evaluate(_valid())
    assert (
        result.eligible and result.reason is Reason.ELIGIBLE and result.tier == "gold"
    )


def test_fresh_physical_security_award_within_7_days_is_platinum() -> None:
    """A verified physical-security award within a week presents as platinum."""
    result = policy.evaluate(_valid(award_date="2026-07-18", program_fit_ok=True))
    assert result.eligible and result.tier == "platinum"


def test_recent_award_without_program_fit_stays_gold() -> None:
    """Platinum requires the strong physical-security program rule, not just recency."""
    result = policy.evaluate(_valid(award_date="2026-07-18", program_fit_ok=False))
    assert result.eligible and result.tier == "gold"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("lead_grade", "silver", Reason.NOT_GOLD),
        ("event_type", "rfp_posted", Reason.NOT_AWARD_EVENT),
        ("event_type", "application_window_opened", Reason.NOT_AWARD_EVENT),
        ("event_verified", False, Reason.UNVERIFIED_EVENT),
        ("event_evidence_complete", False, Reason.EVENT_EVIDENCE_MISSING),
        ("amount", None, Reason.BAD_AMOUNT),
        ("amount", 0.0, Reason.BAD_AMOUNT),
        ("amount", -5.0, Reason.BAD_AMOUNT),
        ("amount", float("inf"), Reason.BAD_AMOUNT),
        ("award_date", "", Reason.AWARD_DATE_MISSING),
        ("award_date_precision", "unknown", Reason.AWARD_DATE_MISSING),
        ("award_date", "2027-01-01", Reason.AWARD_DATE_FUTURE),
        ("award_date", "2024-01-01", Reason.AWARD_TOO_OLD),
        ("spend_window_start", "", Reason.WINDOW_CLOSED),
        ("spend_window_start", "2026-08-01", Reason.WINDOW_CLOSED),
        ("spend_window_end", "", Reason.WINDOW_CLOSED),
        ("spend_window_end", "2026-01-01", Reason.WINDOW_CLOSED),
        ("last_confirmed_run_complete", False, Reason.STALE_OBSERVATION),
        ("last_confirmed_at", "2026-07-01", Reason.STALE_OBSERVATION),
        ("last_confirmed_at", "", Reason.STALE_OBSERVATION),
        ("entity_kind", "nonprofit", Reason.KIND_UNSUPPORTED),
        ("entity_kind", "city", Reason.KIND_DEFERRED),
        ("entity_kind_provenance", "", Reason.KIND_PROVENANCE),
        ("entity_kind_provenance", "heuristic", Reason.KIND_PROVENANCE),
        ("state_verified", False, Reason.STATE_PROVENANCE),
        ("award_url_safe", False, Reason.AWARD_URL_UNSAFE),
        ("official_website", "", Reason.NO_WEBSITE),
        # A reviewed directory host is never an org's own site → no provenance.
        ("official_website", "https://cde.ca.gov", Reason.WEBSITE_UNVERIFIED),
        ("contact_status", "not_found", Reason.CONTACT_MISSING),
        ("contact_type", "", Reason.CONTACT_MISSING),
        ("contact_last_verified_at", "2026-05-01", Reason.CONTACT_STALE),
        ("contact_expires_at", "2026-07-22T11:59:59+00:00", Reason.CONTACT_STALE),
        ("contact_evidence_url_safe", False, Reason.CONTACT_URL_UNSAFE),
        # Ambiguous is NOT here — it is eligible as a research-needed card (below).
        ("crm_state", "partial", Reason.CRM_UNSAFE),
        ("crm_state", "unavailable", Reason.CRM_UNSAFE),
        ("crm_checked_at", "2026-07-20T23:59:59+00:00", Reason.CRM_UNSAFE),
    ],
)
def test_each_rule_rejects_with_its_reason(
    field: str, value: object, reason: Reason
) -> None:
    """Every eligibility rule fails closed with a distinct, machine-usable reason."""
    result = policy.evaluate(_valid(**{field: value}))
    assert not result.eligible and result.reason is reason, f"{field}={value!r}"


def test_personal_mailbox_is_rejected_even_on_an_official_domain_field() -> None:
    """A gmail address is rejected for this campaign regardless of the page it's on."""
    result = policy.evaluate(
        _valid(
            contact_email="principal@gmail.com",
            contact_evidence_url="https://montebelloschools.net/staff",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_PERSONAL


def test_email_not_bound_to_the_organization_is_rejected() -> None:
    """A verified email that is neither on the org's site nor in an exact authoritative
    record is rejected — the scrape page alone never binds it (critic C1)."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@some-other-vendor.com",
            contact_evidence_url="https://cde.ca.gov/schooldirectory/details?cdscode=1",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_school_qualifies_like_a_district() -> None:
    """A single school (not just a district) qualifies with evidenced kind."""
    result = policy.evaluate(
        _valid(entity_kind="school", entity_kind_provenance="nces")
    )
    assert result.eligible


def test_exact_twelve_month_boundary_is_inclusive() -> None:
    """An award exactly one calendar year old remains eligible."""
    assert policy.evaluate(_valid(award_date="2025-07-22")).eligible


def test_one_day_beyond_twelve_month_boundary_is_rejected() -> None:
    """Calendar-month policy does not silently become a 372-day window."""
    result = policy.evaluate(_valid(award_date="2025-07-21"))
    assert result.reason is Reason.AWARD_TOO_OLD


def test_crm_freshness_is_hour_precise() -> None:
    """A CRM lookup older than 24 hours is stale even on the adjacent date."""
    fresh = policy.evaluate(_valid(crm_checked_at="2026-07-21T12:00:00+00:00"))
    stale = policy.evaluate(_valid(crm_checked_at="2026-07-21T11:59:59+00:00"))
    assert fresh.eligible
    assert stale.reason is Reason.CRM_UNSAFE


def test_city_is_modelled_but_deferred_not_unsupported() -> None:
    """`city` is a supported kind in the model, distinct from an unsupported kind, but
    does not qualify in v1 (deferred until non-heuristic provenance exists)."""
    assert "city" in policy.SUPPORTED_ENTITY_KINDS
    assert "city" not in policy.QUALIFYING_ENTITY_KINDS
    assert policy.evaluate(_valid(entity_kind="city")).reason is Reason.KIND_DEFERRED


# --- Change 1: contact email bound to the ORGANIZATION, not the scrape page ----------


def test_email_matching_verified_org_website_is_eligible() -> None:
    """Chase's corrected test: an email whose domain matches the verified organization
    website is eligible, even when the email was verified on an authoritative directory
    page (cde.ca.gov) whose host differs from the email's."""
    result = policy.evaluate(
        _valid(
            contact_email="eevans@montebelloschools.net",
            contact_evidence_url="https://cde.ca.gov/schooldirectory/details?cdscode=19",
        )
    )
    assert result.eligible
    assert result.contact_binding is ContactBinding.ORG_SITE


def test_email_on_a_subdomain_of_the_org_website_is_eligible() -> None:
    """A district mail subdomain (sd.<district>) still binds to the organization."""
    result = policy.evaluate(_valid(contact_email="eevans@sd.montebelloschools.net"))
    assert result.eligible and result.contact_binding is ContactBinding.ORG_SITE


def test_email_in_exact_nces_record_binds_without_org_site_match() -> None:
    """An email verbatim in an EXACT, id-bound NCES record binds even when its domain
    is not the org website — the exact-id URL provides the organization binding."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@somedistrict.org",
            contact_evidence_url="https://nces.ed.gov/ccd/districtsearch/district_detail.asp?ID=0622710",
        )
    )
    assert result.eligible
    assert result.contact_binding is ContactBinding.AUTHORITATIVE_DIRECTORY


def test_authoritative_directory_binding_requires_the_exact_lead_id() -> None:
    """A reviewed-directory page that does NOT carry this lead's exact id cannot bind —
    a name match is never enough (fail-closed)."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@somedistrict.org",
            contact_evidence_url="https://nces.ed.gov/ccd/districtsearch/district_detail.asp?ID=9999999",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_cde_directory_cannot_exact_bind_today_without_a_stored_code() -> None:
    """We store no CA CDS code, so a cde.ca.gov record cannot exact-bind an off-site
    email — it stays rejected rather than trusting the directory host loosely."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@somedistrict.org",
            contact_evidence_url="https://cde.ca.gov/schooldirectory/details?cdscode=19",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_authoritative_directory_rejects_a_substring_id_collision() -> None:
    """Critic H1: this lead's id `062271` must NOT bind another district's exact record
    `?ID=0622710` — the id is matched as a whole token, never a substring."""
    result = policy.evaluate(
        _valid(
            nces_id="062271",
            contact_email="jdoe@somedistrict.org",
            contact_evidence_url="https://nces.ed.gov/ccd/districtsearch/district_detail.asp?ID=0622710",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_authoritative_directory_rejects_id_embedded_in_a_path_token() -> None:
    """The exact id embedded inside a larger path token (`fake0622710`) does not bind."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@somedistrict.org",
            contact_evidence_url="https://nces.ed.gov/ccd/schoolsearch/fake0622710",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_authoritative_directory_ignores_non_id_query_values() -> None:
    """The exact id must be a real query value/segment, not a substring of another param;
    the actual `ID` param here is a different district, so it must not bind."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@somedistrict.org",
            contact_evidence_url="https://nces.ed.gov/ccd/x?other=90622710X&ID=9999999",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_bare_public_suffix_email_never_binds_an_org_website() -> None:
    """Critic M1: an email at a bare label/public suffix (`admin@net`) cannot bind a
    `.net` organization website — `_same_site` requires a dotted label on both sides."""
    result = policy.evaluate(_valid(contact_email="admin@net"))
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_lookalike_suffix_domain_does_not_bind() -> None:
    """`evilmontebelloschools.net` must never bind `montebelloschools.net` — the
    parent/child test anchors on a leading dot, so a shared suffix is not enough."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@evilmontebelloschools.net",
            contact_evidence_url="https://montebelloschools.net/staff",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


# --- Change 2: typed, non-heuristic website provenance -------------------------------


def test_contact_verified_on_org_page_supplies_website_provenance() -> None:
    """A verified contact on the org's OWN domain establishes the website even when the
    separate org-profile scrape failed (the real Bartlett ISD case)."""
    result = policy.evaluate(
        _valid(
            org_profile_found=False,
            org_profile_evidence_url="",
            contact_email="mhauk@montebelloschools.net",
            contact_evidence_url="https://montebelloschools.net/staff",
        )
    )
    assert result.eligible
    assert result.website_provenance is WebsiteProvenance.VERIFIED_ORG_PAGE


def test_nces_published_website_is_accepted_provenance() -> None:
    """An exact NCES-published official site is accepted website provenance."""
    result = policy.evaluate(
        _valid(
            org_profile_found=False,
            org_profile_evidence_url="",
            contact_evidence_url="https://cde.ca.gov/schooldirectory/details?cdscode=19",
            nces_website="https://montebelloschools.net",
        )
    )
    assert result.eligible
    assert result.website_provenance is WebsiteProvenance.NCES


def test_directory_host_is_never_the_org_website_even_with_a_contact_on_it() -> None:
    """A directory host (cde.ca.gov) is never an org's own site: a contact verified on
    it cannot promote it to the website (the Fairfax safety)."""
    result = policy.evaluate(
        _valid(
            official_website="https://cde.ca.gov",
            org_profile_found=False,
            org_profile_evidence_url="",
            contact_evidence_url="https://cde.ca.gov/schooldirectory/details?cdscode=15",
        )
    )
    assert not result.eligible and result.reason is Reason.WEBSITE_UNVERIFIED


# --- Change 3: Salesforce ambiguity is a research-needed card, never a hard reject ----


def test_ambiguous_fresh_crm_is_research_needed_not_rejected() -> None:
    """Fresh-but-ambiguous CRM is eligible as a research-needed card."""
    result = policy.evaluate(_valid(crm_state="ambiguous"))
    assert result.eligible and result.reason is Reason.ELIGIBLE
    assert result.card_mode is CardMode.RESEARCH_NEEDED


def test_exact_match_and_no_match_are_draft_ready_only_with_proven_website() -> None:
    """With PROVEN (exact NCES) website ownership, exact-match and complete-no-match CRM
    are draft-ready."""
    proven = {"nces_website": "https://montebelloschools.net"}
    assert policy.evaluate(_valid(**proven)).card_mode is CardMode.DRAFT_READY
    assert (
        policy.evaluate(_valid(crm_state="complete_no_match", **proven)).card_mode
        is CardMode.DRAFT_READY
    )


def test_heuristic_website_is_never_draft_ready() -> None:
    """Critic H2 / Chase 2026-07-23: a card whose website ownership rests on a name
    heuristic (verified_org_page) is capped at research-needed even with a clean CRM — no
    auto-draft on an inferred website."""
    result = policy.evaluate(_valid(crm_state="complete_no_match"))  # no nces_website
    assert result.eligible
    assert result.website_provenance is WebsiteProvenance.VERIFIED_ORG_PAGE
    assert result.card_mode is CardMode.RESEARCH_NEEDED


def test_two_districts_under_one_public_suffix_do_not_cross_bind() -> None:
    """A contact at valle.k12.ca.us must not bind a montebello.k12.ca.us website: they
    share the public suffix but have DIFFERENT registrable domains (eTLD+1)."""
    result = policy.evaluate(
        _valid(
            official_website="https://montebello.k12.ca.us",
            nces_website="https://montebello.k12.ca.us",
            org_profile_evidence_url="https://montebello.k12.ca.us/contact",
            contact_email="super@valle.k12.ca.us",
            contact_evidence_url="https://montebello.k12.ca.us/staff",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_bare_multilabel_public_suffix_email_never_binds() -> None:
    """An email at a bare multi-label public suffix (k12.ca.us) has no registrable domain
    and cannot bind an organization website — the case a single-dot guard would miss."""
    result = policy.evaluate(
        _valid(
            official_website="https://montebello.k12.ca.us",
            nces_website="https://montebello.k12.ca.us",
            org_profile_evidence_url="https://montebello.k12.ca.us/contact",
            contact_email="admin@k12.ca.us",
            contact_evidence_url="https://montebello.k12.ca.us/staff",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_ambiguous_but_stale_crm_is_ineligible() -> None:
    """Ambiguity does not excuse staleness: a stale ambiguous lookup is ineligible."""
    result = policy.evaluate(
        _valid(crm_state="ambiguous", crm_checked_at="2026-07-20T23:59:59+00:00")
    )
    assert not result.eligible and result.reason is Reason.CRM_UNSAFE
