"""Bounded LinkedIn search-result presentation for Grant's Slack conversations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import requests

from .. import db, linkedin_candidates
from ..enrich import finder

Progress = Callable[[str], None]


def _crm_action_result(action_id: str, nonce: str, preview: str,
                       expires_at: str) -> str:
    """Encode server-only confirmation metadata for Slack's delivery layer."""
    import json

    marker = json.dumps({
        "action_id": action_id, "nonce": nonce, "preview": preview,
        "expires_at": expires_at,
    }, separators=(",", ":"))
    return f"<grant-crm-action>{marker}</grant-crm-action>"


def find_person_linkedin(
        entity: str, state: str, on_progress: Progress | None = None, *,
        conn: sqlite3.Connection | None = None, lead_id: int = 0,
        workspace: str = "", channel: str = "", thread_ts: str = "",
        requested_by: str = "") -> str:
    """Return and optionally persist one context-bound LinkedIn result without email."""
    person = finder.linkedin_person(entity, state, on_progress=on_progress)
    if person is None:
        return ("I couldn’t find a clear LinkedIn match tied to this organization. "
                "I won’t guess at a person.")
    role = person.title or "role not shown in the search result"
    if conn is not None:
        linkedin_candidates.save_candidate(
            conn, lead_id, workspace, channel, thread_ts, requested_by, entity, person)
    return (
        "I found a possible LinkedIn contact:\n\n"
        f"• *Name:* {person.name}\n"
        f"• *Role:* {role}\n"
        f"• *Profile:* <{person.url}|LinkedIn>\n"
        "• *Verification:* matched in LinkedIn search results; no email verified"
    )


def salesforce_linkedin_person_preview(
        candidate_id: str, requester_slack: str, workspace: str,
        channel: str, thread_ts: str) -> str:
    """Prepare one no-email LinkedIn person create or exact placeholder update."""
    from ..enrich import salesforce_linkedin_actions as linkedin_actions
    from ..enrich.salesforce_campaign_gateway import SalesforceCampaignGateway

    conn = db.connect()
    try:
        action = linkedin_actions.prepare_linkedin_person(
            conn, SalesforceCampaignGateway(), workspace, channel, thread_ts,
            requester_slack, candidate_id)
    except (ValueError, PermissionError, KeyError, ConnectionError,
            requests.RequestException) as exc:
        return f"ERROR: LinkedIn Lead preview failed ({type(exc).__name__}): {str(exc)[:180]}"
    finally:
        conn.close()
    return _crm_action_result(action.action_id, action.nonce,
                              action.preview, action.expires_at)
