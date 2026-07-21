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
    e.suppressed AS current_event_suppressed"""
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
