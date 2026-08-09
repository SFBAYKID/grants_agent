"""Slack tool surface for guarded Salesforce Campaign previews and batches."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

import requests

from .. import db
from .. import persequor_client
from ..enrich import salesforce_campaigns as campaigns
from ..enrich.salesforce_campaign_batch import prepare_campaign_batch
from ..enrich.salesforce_campaign_batch_models import CampaignTargetRequest
from ..enrich.salesforce_campaign_gateway import SalesforceCampaignGateway
from ..enrich.salesforce_campaign_models import PreparedAction

CAMPAIGN_CREATE_TOOL_SCHEMA: dict[str, Any] = {
    "name": "salesforce_campaign_create_preview",
    "description": (
        "Prepare, but do not execute, one immutable Salesforce Campaign preview. "
        "Call exactly once only after the user explicitly supplied or confirmed the "
        "name, Type, Status, Active value, and either both dates or no dates. A name "
        "alone is never sufficient; never infer defaults."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "campaign_type": {"type": "string"},
            "status": {"type": "string"},
            "is_active": {"type": "boolean"},
            "date_mode": {"type": "string", "enum": ["none", "range"]},
            "start_date": {
                "type": "string",
                "description": "YYYY-MM-DD; required only when date_mode is range",
            },
            "end_date": {
                "type": "string",
                "description": "YYYY-MM-DD; required only when date_mode is range",
            },
            "description": {"type": "string"},
        },
        "required": [
            "name",
            "campaign_type",
            "status",
            "is_active",
            "date_mode",
        ],
    },
}

CAMPAIGN_BATCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "salesforce_campaign_batch_preview",
    "description": (
        "Freeze complete Grant state/tier selections and prepare one isolated, audited "
        "Salesforce Campaign approval per target. Use this instead of exporting IDs or "
        "calling the single-list tool repeatedly when the user asks for one or more "
        "whole state/tier lists. The first call must leave both approval flags false."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "campaign_link": {"type": "string"},
                        "state": {"type": "string"},
                        "grades": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["gold", "silver", "watch"],
                            },
                            "minItems": 1,
                            "maxItems": 3,
                        },
                        "slice_index": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Zero-based batch number. Salesforce "
                            "takes at most 200 organizations at a time, so a large "
                            "state and tier is split into ordered batches. Omit "
                            "(or 0) for the first; the preview tells you how many "
                            "there are, and you run the next by asking for "
                            "slice_index 1, then 2. NEVER tell the rep the campaign "
                            "is complete until every batch has been approved.",
                        },
                    },
                    "required": ["campaign_link", "state", "grades"],
                },
            },
            "allow_org_leads": {"type": "boolean", "default": False},
            "allow_resolved_only": {"type": "boolean", "default": False},
        },
        "required": ["targets"],
    },
}


def _campaign_creation_fields(args: dict[str, Any]) -> dict[str, object]:
    """Validate explicit model arguments before any Salesforce or database access."""
    required = ("name", "campaign_type", "status", "is_active", "date_mode")
    missing = [key for key in required if key not in args]
    if missing:
        raise ValueError(
            "Campaign preview needs explicit settings: " + ", ".join(missing)
        )
    name = str(args["name"]).strip()
    campaign_type = str(args["campaign_type"]).strip()
    status = str(args["status"]).strip()
    if not name or not campaign_type or not status:
        raise ValueError("Campaign name, Type, and Status must be explicit")
    if type(args["is_active"]) is not bool:
        raise ValueError("Campaign Active must be an explicit boolean")
    date_mode = str(args["date_mode"]).strip().lower()
    start_date = str(args.get("start_date", "")).strip()
    end_date = str(args.get("end_date", "")).strip()
    if date_mode == "none":
        if start_date or end_date:
            raise ValueError("No-dates Campaign previews cannot include dates")
    elif date_mode == "range":
        if not start_date or not end_date:
            raise ValueError("Date-range Campaign previews need both dates")
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("Campaign dates must use YYYY-MM-DD") from exc
        if start.isoformat() != start_date or end.isoformat() != end_date:
            raise ValueError("Campaign dates must use YYYY-MM-DD")
        if start > end:
            raise ValueError("Campaign start date cannot be after end date")
    else:
        raise ValueError("Campaign date_mode must be none or range")
    return {
        "name": name,
        "campaign_type": campaign_type,
        "status": status,
        "is_active": args["is_active"],
        "start_date": start_date,
        "end_date": end_date,
        "description": str(args.get("description", "")),
    }


def salesforce_campaign_create_preview(
    args: dict[str, Any],
    requester_slack: str,
    workspace: str,
    channel: str,
    thread_ts: str,
) -> str:
    """Prepare one fully specified requester-bound Campaign creation preview."""
    try:
        fields = _campaign_creation_fields(args)
    except ValueError as exc:
        return f"ERROR: Campaign preview failed (ValueError): {str(exc)[:180]}"
    gateway = SalesforceCampaignGateway()
    owner_id = ""
    owner_label = "Salesforce integration user"
    try:
        requester_email = persequor_client.rep_email_for(requester_slack) or ""
        owners = gateway.find_active_user_by_email(requester_email)
        if len(owners) == 1:
            owner_id = owners[0].record_id
            owner_label = owners[0].name
        action = campaigns.prepare_campaign_creation(
            db.connect(),
            gateway,
            workspace,
            channel,
            thread_ts,
            requester_slack,
            campaigns.CampaignDraft(
                owner_id=owner_id,
                owner_label=owner_label,
                **fields,
            ),
        )
    except (
        ValueError,
        PermissionError,
        KeyError,
        sqlite3.IntegrityError,
        requests.RequestException,
    ) as exc:
        return (
            f"ERROR: Campaign preview failed ({type(exc).__name__}): {str(exc)[:180]}"
        )
    return f"{action.preview}\n{_action_marker(action)}"


def _action_marker(action: PreparedAction) -> str:
    """Encode one server-only child approval marker for Slack block rendering."""
    marker = json.dumps(
        {
            "action_id": action.action_id,
            "nonce": action.nonce,
            "preview": action.preview,
            "expires_at": action.expires_at,
        },
        separators=(",", ":"),
    )
    return f"<grant-crm-action>{marker}</grant-crm-action>"


def salesforce_campaign_batch_preview(
    args: dict[str, Any],
    requester_slack: str,
    workspace: str,
    channel: str,
    thread_ts: str,
) -> str:
    """Prepare a complete durable batch or return a non-executable blocked summary."""
    requests_list: list[CampaignTargetRequest] = []
    for target in args.get("targets", []) or []:
        if not isinstance(target, dict):
            continue
        requests_list.append(
            CampaignTargetRequest(
                campaign_link=str(target.get("campaign_link", "")),
                state=str(target.get("state", "")),
                grades=tuple(str(item) for item in target.get("grades", []) or []),
                slice_index=int(target.get("slice_index", 0) or 0),
            )
        )
    try:
        batch = prepare_campaign_batch(
            db.connect(),
            SalesforceCampaignGateway(),
            workspace,
            channel,
            thread_ts,
            requester_slack,
            tuple(requests_list),
            allow_org_leads=bool(args.get("allow_org_leads", False)),
            allow_resolved_only=bool(args.get("allow_resolved_only", False)),
        )
    except (ValueError, PermissionError, KeyError, requests.RequestException) as exc:
        return (
            "ERROR: Campaign batch preview failed "
            f"({type(exc).__name__}): {str(exc)[:240]}"
        )
    markers = "\n".join(_action_marker(action) for action in batch.actions)
    return f"{batch.summary}\n{markers}".strip()
