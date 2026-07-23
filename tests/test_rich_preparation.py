"""Write-free preparation and PII-free shadow report tests using SQLite fixtures."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from grant_watch import db
from grant_watch.campaign import report
from grant_watch.campaign.policy import Reason
from grant_watch.campaign.preparation import review_candidates

NOW = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)


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
              last_confirmed_at,current_event_id)
           VALUES (1,'usaspending:16.071','award-123','gold','Montebello USD','district',
                   'CA','SVPP',500000,'2025-10-10','2028-09-30',
                   'https://www.usaspending.gov/award/award-123','new',
                   'montebello usd|CA','0625260','https://montebello.k12.ca.us','found',
                   4,'2026-07-22T17:05:00+00:00',2)"""
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
        """INSERT INTO funding_events
             (id,lead_id,observation_id,event_type,occurred_on,date_precision,amount,
              source_url,verification_status,backfill,suppressed,created_at)
           VALUES (2,1,3,'award_obligated','2026-06-01','day',500000,
                   'https://www.usaspending.gov/award/award-123','verified',0,0,
                   '2026-07-22T17:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO contact_evidence
             (id,lead_id,status,contact_type,name,title,email,official_evidence_url,
              official_domain,evidence_hash,first_verified_at,last_checked_at,
              last_verified_at,expires_at)
           VALUES ('contact-1',1,'verified','named_direct','Jon Smith','IT Director',
                   'jon@montebello.k12.ca.us','https://montebello.k12.ca.us/staff',
                   'montebello.k12.ca.us','contact-hash','2026-07-20T00:00:00+00:00',
                   '2026-07-22T17:00:00+00:00','2026-07-22T17:00:00+00:00',
                   '2026-08-21T17:00:00+00:00')"""
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
    assert "salesforce" not in rendered.lower()
    assert "montebello" not in rendered.lower()
