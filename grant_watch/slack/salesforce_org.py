"""Slack facade for contact-independent Salesforce organization enrichment."""

from __future__ import annotations

import json

import requests

from .. import db
from ..enrich import salesforce_campaigns as crm
from ..enrich import salesforce_org_enrichment as org_enrichment
from ..enrich import salesforce_record_actions as records


def _crm_action_result(action_id: str, nonce: str, preview: str,
                       expires_at: str) -> str:
    """Return a human preview plus a server-only action marker."""
    return (
        f"{preview}\n\n"
        "<grant-crm-action>"
        f'{{"action_id":"{action_id}","nonce":"{nonce}",'
        f'"preview":{json.dumps(preview)},'
        f'"expires_at":"{expires_at}"}}'
        "</grant-crm-action>"
    )


def salesforce_organization_lead_enrichment_preview(
        grant_lead_id: int, requester_slack: str, workspace: str,
        channel: str, thread_ts: str) -> str:
    """Prepare one exact existing Lead's organization fields without an email."""
    conn = db.connect()
    try:
        row = db.get_lead(conn, grant_lead_id)
        if row is None:
            raise ValueError("Grant lead is stale or unknown")
        company = str(row["entity_name"] or "").strip()
        state = str(row["state"] or "").strip().upper()
        exact = org_enrichment.select_exact_lead(
            records.duplicate_organization(company, state), company, state)
        action = crm.prepare_organization_lead_enrichment(
            conn, crm.SalesforceCampaignGateway(), workspace, channel, thread_ts,
            requester_slack, grant_lead_id, exact.link)
    except (ValueError, PermissionError, KeyError, ConnectionError,
            requests.RequestException) as exc:
        return ("ERROR: Organization Lead enrichment preview failed "
                f"({type(exc).__name__}): {str(exc)[:180]}")
    finally:
        conn.close()
    return _crm_action_result(
        action.action_id, action.nonce, action.preview, action.expires_at)
