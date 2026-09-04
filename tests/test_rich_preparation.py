"""Write-free preparation and PII-free shadow report tests using SQLite fixtures."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from grant_watch import db
from grant_watch.campaign import contact_evidence, report
from grant_watch.campaign.policy import Reason
from grant_watch.campaign.preparation import (
    _fresh_activity,
    preparable_lead_ids,
    review_candidates,
)
from grant_watch.campaign.routing import RoutingReason
from tests.contact_support import verified_contact_evidence

NOW = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)


def _make_ambiguous(conn: sqlite3.Connection, *, with_owned_account: bool) -> None:
    """Turn the eligible fixture's CRM into an ambiguous 'found' result.

    Two shapes: no single Account (all Lead matches), and — the dangerous one — a
    SINGLE owned Account with MULTIPLE open Opportunities, which still resolves to
    ambiguous but carries a real Account owner that must never leak into routing."""
    conn.execute("UPDATE salesforce_lookup_state SET status='found'")
    if with_owned_account:
        # A REAL roster owner (Brett) who is a channel member: without the research
        # guard, the Account owner out-prioritizes territory and would win the route.
        conn.execute(
            """INSERT INTO salesforce_matches
                 (lead_id,sobject,record_id,name,link,confidence,account_id,is_closed,
                  owner_id,owner_email,checked_at)
               VALUES (1,'Account','001A','Acct','https://sf.test/a','high',NULL,0,
                       'U08C1NBH875','brett@monarchconnected.com',
                       '2026-07-22T17:30:00+00:00')"""
        )
        for opp in ("006A", "006B"):
            conn.execute(
                """INSERT INTO salesforce_matches
                     (lead_id,sobject,record_id,name,link,confidence,account_id,
                      is_closed,checked_at)
                   VALUES (1,'Opportunity',?,?,?, 'high','001A',0,
                           '2026-07-22T17:30:00+00:00')""",
                (opp, f"Opp {opp}", f"https://sf.test/o/{opp}"),
            )
    else:
        for rid in ("00Q1", "00Q2"):
            conn.execute(
                """INSERT INTO salesforce_matches
                     (lead_id,sobject,record_id,name,link,confidence,checked_at)
                   VALUES (1,'Lead',?,?,?, 'high','2026-07-22T17:30:00+00:00')""",
                (rid, f"Lead {rid}", f"https://sf.test/l/{rid}"),
            )
    conn.commit()


def _eligible_conn(path: Path) -> sqlite3.Connection:
    """Create one complete award/contact/run/CRM-no-match fixture."""
    conn = db.connect(path)
    conn.execute(
        """INSERT INTO runs(id,started,finished,source,items_seen,items_new,errors,
                            complete,state)
           VALUES (4,'2026-07-22T17:00:00+00:00','2026-07-22T17:05:00+00:00',
                   'usaspending:16.071',1,1,NULL,1,'complete')"""
    )
    conn.execute(
        """INSERT INTO leads
             (id,source,source_item_id,lead_grade,entity_name,entity_type,state,program,
              amount,funds_start,funds_end,detail_url,status,canonical_entity_key,
              nces_id,org_website,org_profile_status,last_confirmed_run_id,
              last_confirmed_at,current_event_id,org_profile_source_url,nces_website,
              nces_website_source_url,nces_website_status)
           VALUES (1,'usaspending:16.071','award-123','gold','Montebello USD','district',
                   'CA','SVPP',500000,'2025-10-10','2028-09-30',
                   'https://www.usaspending.gov/award/award-123','new',
                   'montebello usd|CA','0625260','https://montebello.k12.ca.us','found',
                   4,'2026-07-22T17:05:00+00:00',2,
                   'https://montebello.k12.ca.us/about',
                   'https://montebello.k12.ca.us',
                   'https://nces.ed.gov/ccd/districtsearch/district_detail.asp?ID2=0625260',
                   'verified')"""
    )
    conn.execute(
        """INSERT INTO source_observations
             (id,lead_id,source,source_item_id,observed_at,payload_hash,raw_json,
              source_url,source_locator,verification_status)
           VALUES (3,1,'usaspending:16.071','award-123','2026-07-22T17:00:00+00:00',
                   'hash','{}','https://www.usaspending.gov/award/award-123','award-123',
                   'verified')"""
    )
    conn.execute(
        """INSERT INTO organization_field_evidence
             (id,lead_id,field_name,field_value,source_url,excerpt,evidence_hash,
              verifier_version,status,verified_at)
           VALUES ('org-site-1',1,'website','https://montebello.k12.ca.us',
                   'https://montebello.k12.ca.us/about','Montebello USD contact page',
                   'org-site-hash','field-evidence-v2','current',
                   '2026-07-22T17:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO funding_events
             (id,lead_id,observation_id,event_type,occurred_on,date_precision,amount,
              source_url,verification_status,backfill,suppressed,created_at)
           VALUES (2,1,3,'award_obligated','2026-06-01','day',500000,
                   'https://www.usaspending.gov/award/award-123','verified',0,0,
                   '2026-07-22T17:00:00+00:00')"""
    )
    contact_url = "https://montebello.k12.ca.us/staff"
    contact_fact = contact_evidence.ContactFact(
        "named_direct",
        "Jon Smith",
        "IT Director",
        "jon@montebello.k12.ca.us",
        contact_url,
        "montebello.k12.ca.us",
        verified_contact_evidence(
            "Jon Smith",
            "jon@montebello.k12.ca.us",
            contact_url,
            title="IT Director",
        ),
    )
    conn.execute(
        """INSERT INTO contact_evidence
             (id,lead_id,status,contact_type,name,title,email,official_evidence_url,
              official_domain,evidence_hash,first_verified_at,last_checked_at,
              last_verified_at,expires_at,field_evidence_json)
           VALUES ('contact-1',1,'verified','named_direct','Jon Smith','IT Director',
                   'jon@montebello.k12.ca.us','https://montebello.k12.ca.us/staff',
                   'montebello.k12.ca.us',?,'2026-07-20T00:00:00+00:00',
                   '2026-07-22T17:00:00+00:00','2026-07-22T17:00:00+00:00',
                   '2026-08-21T17:00:00+00:00',?)""",
        (
            contact_evidence.fact_hash(contact_fact),
            contact_evidence.serialize_fact_evidence(contact_fact),
        ),
    )
    conn.execute(
        """INSERT INTO salesforce_lookup_state(lead_id,status,checked_at)
           VALUES (1,'no_match','2026-07-22T17:30:00+00:00')"""
    )
    conn.commit()
    return conn


def test_complete_persisted_evidence_builds_one_routed_draft(tmp_path: Path) -> None:
    """A strict fixture becomes a card and routes through verified CA territory."""
    conn = _eligible_conn(tmp_path / "ready.db")
    before = conn.total_changes
    reviews = review_candidates(
        conn,
        "CGRANTS",
        frozenset({"U01DFJWQQJ3"}),
        now=NOW,
    )
    assert conn.total_changes == before
    assert len(reviews) == 1 and reviews[0].reason is Reason.ELIGIBLE
    draft = reviews[0].draft
    assert draft is not None
    assert draft.event_id == 2 and draft.observation_id == 3 and draft.run_id == 4
    assert draft.entity_kind == "school_district"
    assert draft.route.slack_user_id == "U01DFJWQQJ3"
    assert draft.contact_evidence_id == "contact-1"
    assert "jon@montebello.k12.ca.us" in draft.fallback_text


def test_ambiguous_crm_builds_a_research_card_routed_by_territory(
    tmp_path: Path,
) -> None:
    """A fresh but ambiguous CRM lookup is eligible as a research-needed card: territory
    routing, the review banner, and no relationship/net-new claim."""
    conn = _eligible_conn(tmp_path / "amb.db")
    _make_ambiguous(conn, with_owned_account=False)
    reviews = review_candidates(conn, "CGRANTS", frozenset({"U01DFJWQQJ3"}), now=NOW)
    assert len(reviews) == 1 and reviews[0].reason is Reason.ELIGIBLE
    draft = reviews[0].draft
    assert draft is not None
    assert draft.card_mode == "research_needed"
    assert draft.route.reason is RoutingReason.TERRITORY
    assert draft.route.slack_user_id == "U01DFJWQQJ3"
    assert (
        draft.sf_display_text == "Possible Salesforce matches—review before outreach."
    )
    assert draft.sf_account_id == "" and draft.sf_open_link == ""
    assert "net-new" not in draft.fallback_text.lower()


def test_ambiguous_owned_account_never_leaks_its_owner_into_routing(
    tmp_path: Path,
) -> None:
    """The dangerous ambiguous shape — a single OWNED Account with multiple open
    Opportunities — must route by territory, never to that Account's Salesforce owner."""
    conn = _eligible_conn(tmp_path / "amb-owned.db")
    _make_ambiguous(conn, with_owned_account=True)
    reviews = review_candidates(
        conn,
        "CGRANTS",
        # Both the territory rep AND the (wrong) account owner are channel members, so a
        # leak would actually route to the owner if the guard were missing.
        frozenset({"U01DFJWQQJ3", "U08C1NBH875"}),
        now=NOW,
    )
    draft = reviews[0].draft
    assert draft is not None
    assert draft.card_mode == "research_needed"
    assert draft.route.reason is RoutingReason.TERRITORY
    assert draft.route.slack_user_id == "U01DFJWQQJ3"
    assert draft.sf_account_id == ""


def test_heuristic_website_caps_exact_crm_at_research_but_keeps_owner_routing(
    tmp_path: Path,
) -> None:
    """A clean EXACT Salesforce relationship with a HEURISTIC (non-exact) website is
    research-needed — it cannot auto-draft — YET keeps its real Account owner routing and
    account id. The CRM drop must NOT over-broaden beyond the ambiguous case (critic
    round-2 non-blocking observation, locked in here)."""
    conn = _eligible_conn(tmp_path / "exact-heuristic.db")
    # Heuristic website: remove the exact NCES site so provenance is verified_org_page.
    conn.execute(
        "UPDATE leads SET nces_website=NULL,nces_website_status=NULL WHERE id=1"
    )
    # One EXACT Salesforce Account owned by a channel-member rep (Brett).
    conn.execute("UPDATE salesforce_lookup_state SET status='found'")
    conn.execute(
        """INSERT INTO salesforce_matches
             (lead_id,sobject,record_id,name,link,confidence,account_id,is_closed,
              owner_id,owner_email,checked_at)
           VALUES (1,'Account','001EXACT','Acct',
                   'https://sf.test/lightning/r/Account/001/view','high',NULL,0,
                   'U08C1NBH875','brett@monarchconnected.com',
                   '2026-07-22T17:30:00+00:00')"""
    )
    conn.commit()
    reviews = review_candidates(conn, "CGRANTS", frozenset({"U08C1NBH875"}), now=NOW)
    draft = reviews[0].draft
    assert draft is not None
    assert (
        draft.card_mode == "research_needed"
    )  # heuristic website caps it (no auto-draft)
    assert draft.official_website_provenance == "verified_org_page"
    assert (
        draft.route.reason is RoutingReason.SF_ACCOUNT_OWNER
    )  # real relationship kept
    assert draft.route.slack_user_id == "U08C1NBH875"
    assert (
        draft.sf_account_id == "001EXACT"
    )  # NOT dropped (only the ambiguous path drops)
    assert draft.sf_open_link  # the account link is preserved
    assert draft.sf_display_text == "Exact Account match."


def test_legacy_unbound_org_projection_cannot_enable_a_draft_action(
    tmp_path: Path,
) -> None:
    """Pre-migration website columns remain visible data, not verification evidence."""
    conn = _eligible_conn(tmp_path / "legacy-unbound.db")
    conn.execute(
        "UPDATE leads SET nces_website=NULL,nces_website_status=NULL WHERE id=1"
    )
    conn.execute("DELETE FROM organization_field_evidence WHERE lead_id=1")
    conn.commit()

    review = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)[0]

    assert review.draft is None
    assert review.reason is Reason.NO_WEBSITE


def test_verified_nces_site_supplies_the_official_website_without_org_scrape(
    tmp_path: Path,
) -> None:
    """The exact district record makes the draft action reachable in real assembly."""
    conn = _eligible_conn(tmp_path / "nces-website.db")
    conn.execute(
        "UPDATE leads SET org_website='',org_profile_status='',org_profile_source_url=''"
    )
    conn.commit()

    review = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)[0]

    assert review.draft is not None
    assert review.draft.official_website == "https://montebello.k12.ca.us"
    assert review.draft.official_website_provenance == "nces"
    assert review.draft.card_mode == "draft_ready"
    assert "nces.ed.gov/ccd/districtsearch" in (
        review.draft.official_website_evidence_url
    )
    conn.close()


def test_card_amount_and_event_meaning_come_from_exact_event(tmp_path: Path) -> None:
    """Mutable projection drift cannot change the frozen event's amount or meaning."""
    conn = _eligible_conn(tmp_path / "event-truth.db")
    conn.execute("UPDATE leads SET amount=1 WHERE id=1")
    conn.execute(
        "UPDATE funding_events SET event_type='award_announced',evidence_excerpt='DOJ announcement' WHERE id=2"
    )
    conn.commit()
    draft = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)[0].draft
    assert draft is not None
    assert draft.amount == 500_000
    assert draft.event_type == "award_announced"
    assert draft.event_evidence_excerpt == "DOJ announcement"


def test_generic_award_page_is_not_labelled_an_exact_record(tmp_path: Path) -> None:
    """A safe homepage remains ineligible when it is not the exact award locator."""
    conn = _eligible_conn(tmp_path / "generic-url.db")
    conn.execute(
        "UPDATE funding_events SET source_url='https://www.usaspending.gov/' WHERE id=2"
    )
    conn.commit()
    review = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)[0]
    assert review.reason is Reason.AWARD_URL_UNSAFE and review.draft is None


def test_snapshot_expires_when_spend_window_closes_before_contact(
    tmp_path: Path,
) -> None:
    """A long-lived contact cannot extend a card beyond its evidenced spend window."""
    conn = _eligible_conn(tmp_path / "expiry.db")
    conn.execute("UPDATE leads SET funds_end='2026-07-22' WHERE id=1")
    conn.execute(
        "UPDATE contact_evidence SET expires_at='2027-01-01T00:00:00+00:00' WHERE lead_id=1"
    )
    conn.commit()
    draft = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)[0].draft
    assert draft is not None
    assert draft.expires_at.startswith("2026-07-22T23:59:59.999999")


def test_saved_call_must_match_current_account_and_person(tmp_path: Path) -> None:
    """Fresh old-Account activity cannot claim or route after CRM binding changes."""
    conn = _eligible_conn(tmp_path / "activity-binding.db")
    conn.execute(
        """INSERT INTO salesforce_activity_snapshots
             (id,lead_id,status,activity_id,activity_type,completed_at,account_id,
              person_id,owner_user_id,owner_email,owner_slack_id,roster_status,checked_at)
           VALUES ('call',1,'verified_call','00T000000000001AAA','Call',
                   '2026-07-20T00:00:00+00:00','001OLD000000001AAA',
                   '003OLD000000001AAA','005000000000001AAA',
                   'anthony@monarchconnected.com','U01DFJWQQJ3','exact',
                   '2026-07-22T17:30:00+00:00')"""
    )
    conn.commit()
    assert (
        _fresh_activity(
            conn,
            1,
            "001NEW000000001AAA",
            frozenset({"003NEW000000001AAA"}),
            NOW,
        )
        is None
    )


def test_removed_or_expired_contact_rejects_instead_of_using_older_fact(
    tmp_path: Path,
) -> None:
    """The latest lifecycle row wins; stale contact evidence is never resurrected."""
    conn = _eligible_conn(tmp_path / "stale.db")
    conn.execute("UPDATE contact_evidence SET expires_at='2026-07-22T17:59:59+00:00'")
    conn.commit()
    review = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)[0]
    assert review.reason is Reason.CONTACT_STALE and review.draft is None


def test_incomplete_crm_state_is_ineligible(tmp_path: Path) -> None:
    """Unavailable is not complete no-match and cannot support a net-new statement."""
    conn = _eligible_conn(tmp_path / "crm.db")
    conn.execute("UPDATE salesforce_lookup_state SET status='unavailable'")
    conn.commit()
    review = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)[0]
    assert review.reason is Reason.CRM_UNSAFE


def test_shadow_report_is_deterministic_and_contains_no_contact_or_crm_pii(
    tmp_path: Path,
) -> None:
    """Aggregate output exposes counts/ids only, never email or Salesforce detail."""
    conn = _eligible_conn(tmp_path / "report.db")
    reviews = review_candidates(conn, "CGRANTS", frozenset(), now=NOW)
    rendered = report.to_json(report.build(reviews))
    assert rendered == report.to_json(report.build(reviews))
    assert '"ready_card_count": 1' in rendered
    assert "@" not in rendered
    assert '"salesforce_ready": 1' in rendered
    assert '"contact_ready": 1' in rendered
    assert '"organization_kind_ready": 1' in rendered
    assert '"source_run_link_ready": 1' in rendered
    assert '"mapped_route_ready": 0' in rendered
    assert '"unassigned_route_ready": 1' in rendered
    assert "montebello" not in rendered.lower()


def test_the_paid_queue_never_spends_on_an_award_past_the_ceiling(
    tmp_path: Path,
) -> None:
    """`rich-prepare --execute` buys Firecrawl scrapes and ZoomInfo credits for every
    lead `preparable_lead_ids` returns. Production had bought a contact refresh for
    the same eleven-month-old lead on ten separate mornings. Past
    `scoring.CARD_MAX_AWARD_MONTHS` the review's first rejection is AWARD_TOO_OLD,
    which is not remediable, so the queue is empty — and the CONTROL is the same
    fixture inside the ceiling, where it is queued."""
    conn = _eligible_conn(tmp_path / "old.db")
    assert preparable_lead_ids(conn, "C1", now=NOW) == (1,), "control: queued"
    # The production shape: the award is old but the OBSERVATION is fresh (the poller
    # re-confirmed it this morning). Judged at the same NOW, so nothing else in the
    # fixture can be the reason — a later clock would have let STALE_OBSERVATION carry
    # the assertion instead (architectural-critic, 2026-09-04).
    conn.execute("UPDATE funding_events SET occurred_on='2026-01-01' WHERE id=2")
    conn.commit()
    assert preparable_lead_ids(conn, "C1", now=NOW) == ()
    assert review_candidates(conn, "C1", frozenset(), now=NOW) == ()
    (review,) = review_candidates(conn, "C1", frozenset(), now=NOW, limit=1) or (None,)
    assert review is None, "the row must be gone BEFORE the slice, not rejected after"
