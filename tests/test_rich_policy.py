"""Rich award-card eligibility policy — happy paths and every rejection reason.

Award-path fixtures are HAND-BUILT: the local database has zero verified award events
(all record_observed), so nothing here is "verified against seed data" — the policy is a
pure function judged against constructed evidence, exactly as the design requires.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from grant_watch.campaign import policy
from grant_watch.campaign.policy import CandidateEvidence, Reason

TODAY = date(2026, 7, 22)


def _valid(**over: object) -> CandidateEvidence:
    """A fully-eligible GOLD school-district candidate; override one field per test."""
    base = CandidateEvidence(
        lead_id=1,
        lead_grade="gold",
        event_type="award_obligated",
        event_verified=True,
        amount=500_000.0,
        award_date="2026-06-01",
        award_date_precision="day",
        spend_window_end="2028-09-30",
        last_confirmed_at="2026-07-20",
        last_confirmed_run_complete=True,
        entity_kind="school_district",
        entity_kind_provenance="nces",
        state_verified=True,
        award_url_safe=True,
        official_website="https://montebelloschools.net",
        contact_status="verified",
        contact_type="named_direct",
        contact_email="jdoe@montebelloschools.net",
        contact_official_domain="montebelloschools.net",
        contact_last_verified_at="2026-07-10",
        program_fit_ok=True,
        crm_state="exact_match",
        crm_checked_at="2026-07-22",
        today=TODAY,
    )
    return replace(base, **over)  # type: ignore[arg-type]


def test_valid_gold_school_district_is_eligible() -> None:
    """Provide test-local behavior for valid gold school district is eligible."""
    result = policy.evaluate(_valid())
    assert result.eligible and result.reason is Reason.ELIGIBLE and result.tier == "gold"


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
        ("amount", None, Reason.BAD_AMOUNT),
        ("amount", 0.0, Reason.BAD_AMOUNT),
        ("amount", -5.0, Reason.BAD_AMOUNT),
        ("award_date", "", Reason.AWARD_DATE_MISSING),
        ("award_date_precision", "unknown", Reason.AWARD_DATE_MISSING),
        ("award_date", "2027-01-01", Reason.AWARD_DATE_FUTURE),
        ("award_date", "2024-01-01", Reason.AWARD_TOO_OLD),
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
        ("contact_status", "not_found", Reason.CONTACT_MISSING),
        ("contact_type", "", Reason.CONTACT_MISSING),
        ("contact_last_verified_at", "2026-05-01", Reason.CONTACT_STALE),
        ("crm_state", "ambiguous", Reason.CRM_UNSAFE),
        ("crm_state", "unavailable", Reason.CRM_UNSAFE),
        ("crm_checked_at", "2026-07-01", Reason.CRM_UNSAFE),
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
        _valid(contact_email="principal@gmail.com", contact_official_domain="gmail.com")
    )
    assert not result.eligible and result.reason is Reason.CONTACT_PERSONAL


def test_email_domain_must_match_the_official_domain() -> None:
    """A verified email whose domain isn't the org's official domain is rejected."""
    result = policy.evaluate(
        _valid(
            contact_email="jdoe@some-other-vendor.com",
            contact_official_domain="montebelloschools.net",
        )
    )
    assert not result.eligible and result.reason is Reason.CONTACT_DOMAIN


def test_school_qualifies_like_a_district() -> None:
    """A single school (not just a district) qualifies with evidenced kind."""
    result = policy.evaluate(_valid(entity_kind="school", entity_kind_provenance="nces"))
    assert result.eligible


def test_city_is_modelled_but_deferred_not_unsupported() -> None:
    """`city` is a supported kind in the model, distinct from an unsupported kind, but
    does not qualify in v1 (deferred until non-heuristic provenance exists)."""
    assert "city" in policy.SUPPORTED_ENTITY_KINDS
    assert "city" not in policy.QUALIFYING_ENTITY_KINDS
    assert policy.evaluate(_valid(entity_kind="city")).reason is Reason.KIND_DEFERRED
