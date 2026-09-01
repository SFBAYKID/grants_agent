"""Row-shape fragments and the timestamp helper shared by the persistence modules.

Extracted so db.py (leads, ingest, search/export jobs) and db_engagement.py (human
signals and drip selection) can each stay well under the 1000-line cap without either
importing the other — a cycle that would break on import order.
"""

from __future__ import annotations

from datetime import datetime, timezone

LEAD_EVENT_SELECT = """l.*, e.event_type AS current_event_type,
    e.occurred_on AS current_event_occurred_on,
    e.date_precision AS current_event_date_precision,
    e.verification_status AS current_event_verification_status,
    e.evidence_excerpt AS current_event_evidence_excerpt,
    e.source_url AS current_event_source_url,
    e.source_locator AS current_event_source_locator,
    e.backfill AS current_event_backfill,
    e.suppressed AS current_event_suppressed,
    (SELECT oe.field_value FROM organization_field_evidence oe
      WHERE oe.lead_id=l.id AND oe.field_name='website'
        AND oe.status='current' AND oe.field_value=l.org_website
      ORDER BY oe.verified_at DESC LIMIT 1) AS evidenced_org_website,
    (SELECT oe.field_value FROM organization_field_evidence oe
      WHERE oe.lead_id=l.id AND oe.field_name='general_email'
        AND oe.status='current' AND oe.field_value=l.org_general_email
      ORDER BY oe.verified_at DESC LIMIT 1) AS evidenced_org_general_email,
    (SELECT oe.field_value FROM organization_field_evidence oe
      WHERE oe.lead_id=l.id AND oe.field_name='phone'
        AND oe.status='current' AND oe.field_value=l.org_phone
      ORDER BY oe.verified_at DESC LIMIT 1) AS evidenced_org_phone,
    (SELECT oe.field_value FROM organization_field_evidence oe
      WHERE oe.lead_id=l.id AND oe.field_name='street'
        AND oe.status='current' AND oe.field_value=l.org_street
        AND 1 >= (SELECT COUNT(DISTINCT oa.source_url)
                    FROM organization_field_evidence oa
                   WHERE oa.lead_id=l.id AND oa.status='current'
                     AND oa.field_name IN ('street','city','state','postal_code'))
      ORDER BY oe.verified_at DESC LIMIT 1) AS evidenced_org_street,
    (SELECT oe.field_value FROM organization_field_evidence oe
      WHERE oe.lead_id=l.id AND oe.field_name='city'
        AND oe.status='current' AND oe.field_value=l.org_city
        AND 1 >= (SELECT COUNT(DISTINCT oa.source_url)
                    FROM organization_field_evidence oa
                   WHERE oa.lead_id=l.id AND oa.status='current'
                     AND oa.field_name IN ('street','city','state','postal_code'))
      ORDER BY oe.verified_at DESC LIMIT 1) AS evidenced_org_city,
    (SELECT oe.field_value FROM organization_field_evidence oe
      WHERE oe.lead_id=l.id AND oe.field_name='state'
        AND oe.status='current' AND oe.field_value=l.org_state
        AND 1 >= (SELECT COUNT(DISTINCT oa.source_url)
                    FROM organization_field_evidence oa
                   WHERE oa.lead_id=l.id AND oa.status='current'
                     AND oa.field_name IN ('street','city','state','postal_code'))
      ORDER BY oe.verified_at DESC LIMIT 1) AS evidenced_org_state,
    (SELECT oe.field_value FROM organization_field_evidence oe
      WHERE oe.lead_id=l.id AND oe.field_name='postal_code'
        AND oe.status='current' AND oe.field_value=l.org_postal_code
        AND 1 >= (SELECT COUNT(DISTINCT oa.source_url)
                    FROM organization_field_evidence oa
                   WHERE oa.lead_id=l.id AND oa.status='current'
                     AND oa.field_name IN ('street','city','state','postal_code'))
      ORDER BY oe.verified_at DESC LIMIT 1) AS evidenced_org_postal_code"""
CRM_CONTEXT_SELECT = """
    (SELECT s.status FROM salesforce_lookup_state s
     WHERE s.lead_id=l.id) AS salesforce_status,
    (SELECT m.link FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Opportunity'
       AND m.confidence='high' AND COALESCE(m.is_closed,0)=0
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_opportunity_link,
    (SELECT m.name FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Opportunity'
       AND m.confidence='high' AND COALESCE(m.is_closed,0)=0
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_opportunity_name,
    (SELECT m.owner FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Opportunity'
       AND m.confidence='high' AND COALESCE(m.is_closed,0)=0
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_opportunity_owner,
    (SELECT m.link FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Account' AND m.confidence='high'
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_account_link,
    (SELECT m.owner FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Account' AND m.confidence='high'
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_account_owner"""


def _now() -> str:
    """UTC ISO timestamp — one format everywhere so Postgres migration is painless."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A lead a rep has said they are taking is out of every proactive surface, until a
# human hands it back. Chase, 2026-09-01, after a rep wrote "I'm taking Gobles Public
# Schools" and Grant — correctly, at the time — answered that it had nothing to mark.
#
# IT LIVES HERE BECAUSE THERE ARE FOUR CANDIDATE QUERIES, NOT THREE, AND THE ONE THAT
# IS EASIEST TO MISS IS THE ONE THAT ACTUALLY POSTS. `db_engagement` holds the three
# legacy drip tiers (nugget/rfp/bulletin); `campaign.preparation._rows` holds the RICH
# card, which has been the live posting path since 2026-08-05, and also feeds
# `preparable_lead_ids` — the worker that SPENDS Firecrawl scrapes and ZoomInfo
# credits enriching leads so they can become cards. Filtering only the first three
# would have suppressed the fallback and left the primary path posting the very lead
# a rep had just claimed, plus paying to enrich it (architectural-critic, 2026-09-01).
#
# One constant, referenced by all four, for the reason `campaign.delivery` already
# keeps its `_SKIP_*` strings shared: a reworded literal in one caller is a silent
# failure, and the fifth candidate query somebody writes next quarter must not be
# able to omit this by simply not knowing about it.
UNCLAIMED_LEAD_PREDICATE = """l.id NOT IN (
    SELECT lead_id FROM lead_claims WHERE released_at IS NULL)"""
