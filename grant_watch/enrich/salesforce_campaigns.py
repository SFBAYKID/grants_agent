"""Audited Salesforce Campaign approval and persistence workflow.

Natural-language intent may prepare these actions, but only immutable, requester-bound
Slack confirmations can execute the separate create-only Salesforce gateway.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import requests

from .. import db
from . import salesforce
from .salesforce_campaign_gateway import (
    MAX_ACTION_ORGANIZATIONS,
    MEMBER_STATUS,
    SalesforceCompositeRolledBack,
    SalesforceCampaignGateway,
    SalesforceRecordRef,
    parse_record_link,
    validate_record_id,
)

ACTION_TTL_MINUTES = 15


class CampaignActionState(str, Enum):
    """Durable states for one externally mutating action."""

    READY = "ready"
    COMMITTING = "committing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class CampaignDraft:
    """Fields Grant may create on a new Campaign after a human preview."""

    name: str
    campaign_type: str = "Other"
    status: str = "Planned"
    is_active: bool = True
    owner_id: str = ""
    owner_label: str = "Salesforce integration user"
    start_date: str = ""
    end_date: str = ""
    description: str = ""

    def payload(self, action_id: str, requester: str) -> dict[str, object]:
        """Return the exact Salesforce create fields shown in the preview."""
        provenance = (
            f"Created by Grant. Action {action_id}. Requested by Slack user {requester}."
        )
        description = f"{self.description.strip()}\n{provenance}".strip()
        payload: dict[str, object] = {
            "Name": self.name.strip(),
            "Type": self.campaign_type,
            "Status": self.status,
            "IsActive": self.is_active,
            "Description": description,
        }
        if self.owner_id:
            payload["OwnerId"] = self.owner_id
        if self.start_date:
            payload["StartDate"] = self.start_date
        if self.end_date:
            payload["EndDate"] = self.end_date
        return payload


@dataclass(frozen=True)
class MemberPlan:
    """How one canonical organization will become a Campaign Member."""

    lead_id: int | None
    canonical_entity_key: str
    entity_name: str
    state: str
    operation: str
    salesforce_ref: SalesforceRecordRef | None = None
    proposed_lead: dict[str, object] | None = None
    note: str = ""


@dataclass(frozen=True)
class PreparedAction:
    """Stored action plus the one-time nonce returned only to its requester."""

    action_id: str
    nonce: str
    preview: str
    expires_at: str




@dataclass(frozen=True)
class ActionExecution:
    """Human-readable aggregate of a confirmed action's exact outcomes."""

    state: CampaignActionState
    message: str
    campaign_id: str = ""
    added: int = 0
    already_present: int = 0
    unresolved: int = 0
    failed: int = 0
    unknown: int = 0



def _now() -> datetime:
    """Return an aware UTC clock value for approvals and audit state."""
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    """Serialize an aware timestamp consistently."""
    return value.isoformat(timespec="seconds")


def _stable_json(value: object) -> str:
    """Serialize an immutable preview deterministically for payload hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: str) -> str:
    """Hash nonces and immutable payloads before persistence."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()




def write_channel_allowed(channel: str) -> bool:
    """Allow writes only in configured Grant channels, never arbitrary DMs/channels."""
    configured = os.environ.get("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "")
    values = {item.strip() for item in configured.split(",") if item.strip()}
    if not values:
        fallback = os.environ.get("SLACK_CHANNEL_ID", "").strip()
        values = {fallback} if fallback else set()
    return bool(channel and channel in values)


def writer_enabled() -> bool:
    """Return whether external Campaign writes are explicitly feature-enabled."""
    return os.environ.get("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "0") == "1"


def person_lead_writer_enabled() -> bool:
    """Require a separate explicit gate for standalone person Lead creation."""
    return os.environ.get("SALESFORCE_PERSON_LEAD_WRITES_ENABLED", "0") == "1"


def opportunity_writer_enabled() -> bool:
    """Require a distinct explicit gate for Opportunity creation."""
    return os.environ.get("SALESFORCE_OPPORTUNITY_WRITES_ENABLED", "0") == "1"


def lead_enrichment_writer_enabled() -> bool:
    """Require a separate gate for blank-only existing Lead enrichment."""
    return os.environ.get("SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED", "0") == "1"


def lead_audit_writer_enabled() -> bool:
    """Require a separate gate for visible Grant Notes and administrative Tasks."""
    return os.environ.get("SALESFORCE_GRANT_AUDIT_RECORDS_ENABLED", "0") == "1"


def prepare_opportunity_creation(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        account_link: str, name: str, stage_name: str, close_date: str,
        owner_id: str, owner_name: str, amount: float | None = None) -> PreparedAction:
    """Delegate one Opportunity preview to the single-record action module."""
    from . import salesforce_record_actions as records

    return records.prepare_opportunity_creation(
        conn, gateway, workspace, channel, thread_ts, requester, account_link,
        name, stage_name, close_date, owner_id, owner_name, amount)


def _duplicate_person(email: str, company: str, state: str) -> list[salesforce.SFMatch]:
    """Delegate the reader-only person duplicate check for compatibility/tests."""
    from . import salesforce_record_actions as records

    return records.duplicate_person(email, company, state)


def prepare_person_lead_creation(conn: sqlite3.Connection, workspace: str,
                                 channel: str, thread_ts: str, requester: str,
                                 contact_id: int) -> PreparedAction:
    """Delegate one verified-person Lead preview to the record action module."""
    from . import salesforce_record_actions as records

    return records.prepare_person_lead_creation(
        conn, workspace, channel, thread_ts, requester, contact_id)


def prepare_lead_enrichment(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        contact_id: int, lead_link: str) -> PreparedAction:
    """Delegate blank-only existing Lead enrichment to the record action module."""
    from . import salesforce_record_actions as records

    return records.prepare_lead_enrichment(
        conn, gateway, workspace, channel, thread_ts, requester, contact_id, lead_link)


def prepare_lead_audit_repair(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        lead_link: str) -> PreparedAction:
    """Delegate one exact Lead's missing audit-trail repair preview."""
    from . import salesforce_record_actions as records

    return records.prepare_lead_audit_repair(
        conn, gateway, workspace, channel, thread_ts, requester, lead_link)


def _validate_context(workspace: str, channel: str, thread_ts: str,
                      requester: str) -> None:
    """Validate the immutable Slack action context before storing a preview."""
    if not all((workspace, channel, thread_ts, requester)):
        raise ValueError("Salesforce actions require workspace, channel, thread, and user")
    if not write_channel_allowed(channel):
        raise PermissionError("Salesforce writes are limited to configured Grant channels")


def _store_action(conn: sqlite3.Connection, action_type: str, workspace: str,
                  channel: str, thread_ts: str, requester: str,
                  payload: dict[str, object], campaign_id: str = "",
                  plans: list[MemberPlan] | None = None,
                  action_id: str | None = None) -> tuple[str, str, str]:
    """Persist an immutable preview and return action ID, nonce, and expiry."""
    action_id = action_id or str(uuid.uuid4())
    nonce = secrets.token_urlsafe(24)
    now = _now()
    expires = now + timedelta(minutes=ACTION_TTL_MINUTES)
    payload_json = _stable_json(payload)
    stored_plans = []
    for plan in plans or []:
        proposed = {
            "entity_name": plan.entity_name,
            "state": plan.state,
            "salesforce_ref": asdict(plan.salesforce_ref) if plan.salesforce_ref else None,
            "proposed_lead": plan.proposed_lead,
            "note": plan.note,
        }
        stored_plans.append({
            "lead_id": plan.lead_id,
            "canonical_entity_key": plan.canonical_entity_key,
            "operation": plan.operation,
            "proposed": proposed,
        })
    items_hash = _hash(_stable_json(stored_plans))
    with conn:
        conn.execute(
            """INSERT INTO crm_actions
                 (id,action_type,workspace,channel,thread_ts,requested_by,state,
                  payload_json,payload_hash,items_hash,nonce_hash,expires_at,
                  campaign_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (action_id, action_type, workspace, channel, thread_ts, requester,
             CampaignActionState.READY.value, payload_json, _hash(payload_json),
             items_hash, _hash(nonce), _iso(expires), campaign_id or None,
             _iso(now), _iso(now)),
        )
        for plan, stored in zip(plans or [], stored_plans):
            conn.execute(
                """INSERT INTO crm_action_items
                     (action_id,lead_id,canonical_entity_key,operation,proposed_json,state)
                   VALUES (?,?,?,?,?,'ready')""",
                (action_id, plan.lead_id, plan.canonical_entity_key, plan.operation,
                 _stable_json(stored["proposed"])),
            )
    return action_id, nonce, _iso(expires)


def prepare_campaign_creation(conn: sqlite3.Connection,
                              gateway: SalesforceCampaignGateway,
                              workspace: str, channel: str, thread_ts: str,
                              requester: str, draft: CampaignDraft) -> PreparedAction:
    """Validate and persist a new-Campaign preview without writing Salesforce."""
    _validate_context(workspace, channel, thread_ts, requester)
    if not draft.name.strip() or len(draft.name.strip()) > 80:
        raise ValueError("Campaign name must be between 1 and 80 characters")
    if draft.owner_id:
        validate_record_id(draft.owner_id, "User")
    types, statuses = gateway.campaign_picklists()
    if draft.campaign_type not in types:
        raise ValueError(f"Campaign Type '{draft.campaign_type}' is not active")
    if draft.status not in statuses:
        raise ValueError(f"Campaign Status '{draft.status}' is not active")
    action_seed = str(uuid.uuid4())
    payload = draft.payload(action_seed, requester)
    action_id, nonce, expires = _store_action(
        conn, "create_campaign", workspace, channel, thread_ts, requester,
        {"campaign": payload, "owner_label": draft.owner_label,
         "provenance_seed": action_seed}, action_id=action_seed,
    )
    preview = (
        f"Create Salesforce Campaign *{payload['Name']}*\n"
        f"• Type: {payload['Type']}\n• Status: {payload['Status']}\n"
        f"• Active: {payload['IsActive']}\n• Owner: {draft.owner_label}\n"
        f"• Member status later: {MEMBER_STATUS} (not responded)\n"
        "No Leads or Campaign Members will be added in this step."
    )
    return PreparedAction(action_id, nonce, preview, expires)


def _org_lead_payload(row: sqlite3.Row, requester: str,
                      action_id: str) -> dict[str, object]:
    """Build an honest organization-only Lead with no invented person fields."""
    entity = str(row["entity_name"] or "").strip()
    payload: dict[str, object] = {
        "Company": entity,
        "LastName": entity,
        "Status": "New",
        "LeadSource": "Other",
        "Description": (
            "Created by Grant as an organization-only lead. No individual contact "
            f"has been verified. Grant lead {row['id']}; action {action_id}; "
            f"requested by Slack user {requester}; source {row['detail_url'] or 'not provided'}."
        ),
    }
    if row["state"]:
        payload["State"] = str(row["state"])
    return payload


def _record_matches_organization(record: SalesforceRecordRef,
                                 entity_name: str, state: str) -> bool:
    """Require a supplied/found person record to belong to the Grant organization."""
    if not record.company.strip():
        return False
    expected_name = db.canonical_entity_key(entity_name).partition("|")[0]
    record_name = db.canonical_entity_key(record.company).partition("|")[0]
    if expected_name != record_name:
        return False
    return not (state and record.state and state.upper() != record.state.upper())


def prepare_membership(conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
                       workspace: str, channel: str, thread_ts: str, requester: str,
                       campaign: SalesforceRecordRef, lead_ids: list[int],
                       supplied_links: dict[int, str] | None = None,
                       allow_org_leads: bool = False) -> PreparedAction:
    """Resolve a frozen Grant lead set and persist the exact membership preview."""
    _validate_context(workspace, channel, thread_ts, requester)
    validate_record_id(campaign.record_id, "Campaign")
    unique_ids = list(dict.fromkeys(int(item) for item in lead_ids))
    if not unique_ids or len(unique_ids) > MAX_ACTION_ORGANIZATIONS:
        raise ValueError("Choose between 1 and 200 Grant leads")
    placeholders = ",".join("?" for _ in unique_ids)
    rows = list(conn.execute(
        f"SELECT * FROM leads WHERE id IN ({placeholders}) ORDER BY id", unique_ids,
    ))
    if len(rows) != len(unique_ids):
        raise ValueError("One or more Grant lead IDs are stale or unknown")
    action_seed = str(uuid.uuid4())
    supplied_links = supplied_links or {}
    plans_by_key: dict[str, MemberPlan] = {}
    for row in rows:
        key = str(row["canonical_entity_key"] or db.canonical_entity_key(
            str(row["entity_name"]), str(row["state"] or "")))
        if key in plans_by_key:
            continue
        supplied = supplied_links.get(int(row["id"]))
        supplied_mismatch = False
        candidates: list[SalesforceRecordRef]
        if supplied:
            sobject, record_id = parse_record_link(supplied, {"Lead", "Contact"})
            supplied_record = gateway.get_record(sobject, record_id)
            if _record_matches_organization(
                    supplied_record, str(row["entity_name"]), str(row["state"] or "")):
                candidates = [supplied_record]
            else:
                candidates = []
                supplied_mismatch = True
        else:
            candidates = [candidate for candidate in gateway.find_people(
                str(row["entity_name"]), str(row["state"] or ""))
                if _record_matches_organization(
                    candidate, str(row["entity_name"]), str(row["state"] or ""))]
        if len(candidates) == 1:
            plan = MemberPlan(
                int(row["id"]), key, str(row["entity_name"]), str(row["state"] or ""),
                "existing_record", salesforce_ref=candidates[0],
            )
        elif len(candidates) > 1:
            plan = MemberPlan(
                int(row["id"]), key, str(row["entity_name"]), str(row["state"] or ""),
                "ambiguous", note="Multiple Salesforce Leads/Contacts require selection.",
            )
        elif supplied_mismatch:
            plan = MemberPlan(
                int(row["id"]), key, str(row["entity_name"]), str(row["state"] or ""),
                "unresolved",
                note="Supplied Salesforce record does not match this organization/state.",
            )
        elif allow_org_leads:
            plan = MemberPlan(
                int(row["id"]), key, str(row["entity_name"]), str(row["state"] or ""),
                "create_org_lead",
                proposed_lead=_org_lead_payload(row, requester, action_seed),
                note="No individual contact verified; organization name fills Company and LastName.",
            )
        else:
            plan = MemberPlan(
                int(row["id"]), key, str(row["entity_name"]), str(row["state"] or ""),
                "unresolved",
                note="Provide a Salesforce Lead/Contact link or approve an organization-only Lead.",
            )
        plans_by_key[key] = plan
    plans = list(plans_by_key.values())
    payload = {
        "campaign": asdict(campaign),
        "lead_ids": [plan.lead_id for plan in plans],
        "allow_org_leads": allow_org_leads,
        "member_status": MEMBER_STATUS,
        "provenance_seed": action_seed,
    }
    existing = sum(plan.operation == "existing_record" for plan in plans)
    creating = sum(plan.operation == "create_org_lead" for plan in plans)
    unresolved = sum(plan.operation in {"unresolved", "ambiguous"} for plan in plans)
    if existing + creating == 0:
        raise ValueError(
            "No organizations can be added yet; resolve a Salesforce Lead/Contact "
            "or approve organization-only Leads before confirming")
    action_id, nonce, expires = _store_action(
        conn, "add_campaign_members", workspace, channel, thread_ts, requester,
        payload, campaign.record_id, plans, action_id=action_seed,
    )
    mapping_lines: list[str] = []
    for plan in plans:
        label = f"{plan.entity_name} ({plan.state or '?'})"
        if plan.operation == "existing_record" and plan.salesforce_ref is not None:
            mapping_lines.append(
                f"• {label} → {plan.salesforce_ref.sobject} "
                f"{plan.salesforce_ref.name}: {plan.salesforce_ref.link}")
        elif plan.operation == "create_org_lead":
            mapping_lines.append(
                f"• {label} → create organization-only Lead; no person fields")
        else:
            mapping_lines.append(f"• {label} → skipped: {plan.note}")
    preview = (
        f"Add leads to *{campaign.name}*\n"
        f"• Existing Leads/Contacts: {existing}\n"
        f"• Organization-only Leads to create: {creating}\n"
        f"• Unresolved/ambiguous and skipped: {unresolved}\n"
        f"• Campaign Member status: {MEMBER_STATUS} (not responded)\n"
        f"• Campaign: {campaign.link}\n"
        "Frozen organization mapping:\n" + "\n".join(mapping_lines)
    )
    if creating:
        preview += (
            "\nOrganization-only records use the exact organization for Company and "
            "LastName and leave all person/contact fields blank."
        )
    return PreparedAction(action_id, nonce, preview, expires)


def _load_action(conn: sqlite3.Connection, action_id: str) -> sqlite3.Row:
    """Load one durable action or raise a safe stale-action error."""
    row = conn.execute("SELECT * FROM crm_actions WHERE id=?", (action_id,)).fetchone()
    if row is None:
        raise ValueError("Salesforce action was not found")
    return row


def _authorize_action(conn: sqlite3.Connection, row: sqlite3.Row, nonce: str,
                      workspace: str, channel: str, thread_ts: str,
                      requester: str) -> None:
    """Revalidate immutable context, initiator, nonce, state, and expiry."""
    if row["workspace"] != workspace or row["channel"] != channel:
        raise PermissionError("Salesforce approval context does not match")
    if row["thread_ts"] != thread_ts:
        raise PermissionError("Salesforce approval thread does not match")
    if row["requested_by"] != requester:
        raise PermissionError("Only the initiating user may approve this action")
    if not write_channel_allowed(channel):
        raise PermissionError("Salesforce writes are not enabled in this channel")
    if not secrets.compare_digest(str(row["nonce_hash"]), _hash(nonce)):
        raise PermissionError("Salesforce approval token is invalid")
    if row["state"] != CampaignActionState.READY.value:
        raise ValueError(f"Salesforce action is already {row['state']}")
    if datetime.fromisoformat(str(row["expires_at"])) <= _now():
        raise TimeoutError("Salesforce approval preview expired")
    if _hash(str(row["payload_json"])) != row["payload_hash"]:
        raise ValueError("Salesforce approval payload changed after preview")
    stored_items = [{
        "lead_id": item["lead_id"],
        "canonical_entity_key": item["canonical_entity_key"],
        "operation": item["operation"],
        "proposed": json.loads(str(item["proposed_json"])),
    } for item in conn.execute(
        """SELECT lead_id,canonical_entity_key,operation,proposed_json
             FROM crm_action_items WHERE action_id=? ORDER BY id""", (row["id"],))]
    if _hash(_stable_json(stored_items)) != row["items_hash"]:
        raise ValueError("Salesforce approval item mapping changed after preview")


def cancel_action(conn: sqlite3.Connection, action_id: str, requester: str) -> bool:
    """Cancel a ready action only when requested by its initiating user."""
    with conn:
        cur = conn.execute(
            """UPDATE crm_actions SET state=?,updated_at=?
               WHERE id=? AND requested_by=? AND state=?""",
            (CampaignActionState.CANCELLED.value, _iso(_now()), action_id, requester,
             CampaignActionState.READY.value),
        )
    return cur.rowcount == 1


def stored_action_result(conn: sqlite3.Connection, action_id: str, workspace: str,
                         channel: str, thread_ts: str,
                         requester: str) -> ActionExecution:
    """Return a prior action's persisted result for safe repeated button clicks."""
    row = _load_action(conn, action_id)
    if (row["workspace"] != workspace or row["channel"] != channel
            or row["thread_ts"] != thread_ts or row["requested_by"] != requester):
        raise PermissionError("Salesforce action does not belong to this user/context")
    try:
        state = CampaignActionState(str(row["state"]))
    except ValueError:
        state = CampaignActionState.UNKNOWN
    counts = {str(item[0]): int(item[1]) for item in conn.execute(
        "SELECT state,COUNT(*) FROM crm_action_items WHERE action_id=? GROUP BY state",
        (action_id,),
    )}
    added = counts.get("added", 0)
    already = counts.get("already_present", 0)
    unresolved = counts.get("unresolved", 0)
    failed = counts.get("failed", 0)
    message = (
        f"This Salesforce action is already {state.value}: {added} added, "
        f"{already} already present, {unresolved} unresolved, {failed} failed."
    )
    return ActionExecution(state, message, campaign_id=str(row["campaign_id"] or ""),
                           added=added, already_present=already,
                           unresolved=unresolved, failed=failed)


def _begin_commit(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Compare-and-set READY to COMMITTING so retries/double-clicks cannot duplicate."""
    with conn:
        cur = conn.execute(
            """UPDATE crm_actions SET state=?,approved_at=?,updated_at=?,attempts=attempts+1
               WHERE id=? AND state=?""",
            (CampaignActionState.COMMITTING.value, _iso(_now()), _iso(_now()), row["id"],
             CampaignActionState.READY.value),
        )
    if cur.rowcount != 1:
        raise ValueError("Salesforce action was already claimed or completed")


def _mark_external_write_started(conn: sqlite3.Connection, action_id: str) -> None:
    """Durably record that a Salesforce create request may have reached the network."""
    with conn:
        conn.execute(
            "UPDATE crm_actions SET external_write_started=1,updated_at=? WHERE id=?",
            (_iso(_now()), action_id),
        )


def _finish_action(conn: sqlite3.Connection, action_id: str,
                   state: CampaignActionState, campaign_id: str = "",
                   error: str = "") -> None:
    """Persist the terminal/unknown outcome without deleting audit history."""
    with conn:
        conn.execute(
            """UPDATE crm_actions SET state=?,campaign_id=COALESCE(?,campaign_id),
                      last_error=?,committed_at=?,updated_at=? WHERE id=?""",
            (state.value, campaign_id or None, error or None, _iso(_now()), _iso(_now()),
             action_id),
        )


def confirm_action(conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
                   action_id: str, nonce: str, workspace: str, channel: str,
                   thread_ts: str, requester: str,
                   dry_run: bool = False) -> ActionExecution:
    """Execute a stored create-only action after all approval gates pass."""
    row = _load_action(conn, action_id)
    try:
        _authorize_action(conn, row, nonce, workspace, channel, thread_ts, requester)
    except TimeoutError:
        _finish_action(conn, action_id, CampaignActionState.EXPIRED)
        raise
    _begin_commit(conn, row)
    if dry_run:
        _finish_action(conn, action_id, CampaignActionState.DRY_RUN)
        return ActionExecution(CampaignActionState.DRY_RUN,
                               "Dry run verified the approval; Salesforce was not written.")
    if not writer_enabled():
        if row["action_type"] in {"create_campaign", "add_campaign_members"}:
            _finish_action(conn, action_id, CampaignActionState.FAILED,
                           error="campaign writes feature flag disabled")
            return ActionExecution(CampaignActionState.FAILED,
                                   "Salesforce campaign writes are disabled; nothing was created.")
    if row["action_type"] == "create_person_lead" and not person_lead_writer_enabled():
        _finish_action(conn, action_id, CampaignActionState.FAILED,
                       error="person Lead writes feature flag disabled")
        return ActionExecution(CampaignActionState.FAILED,
                               "Salesforce Lead creation is disabled; nothing was created.")
    if row["action_type"] == "create_opportunity" and not opportunity_writer_enabled():
        _finish_action(conn, action_id, CampaignActionState.FAILED,
                       error="Opportunity writes feature flag disabled")
        return ActionExecution(CampaignActionState.FAILED,
                               "Salesforce Opportunity creation is disabled; nothing was created.")
    if (row["action_type"] == "enrich_existing_lead"
            and not lead_enrichment_writer_enabled()):
        _finish_action(conn, action_id, CampaignActionState.FAILED,
                       error="Lead enrichment updates feature flag disabled")
        return ActionExecution(CampaignActionState.FAILED,
                               "Salesforce Lead enrichment is disabled; nothing changed.")
    if (row["action_type"] in {
            "create_person_lead", "enrich_existing_lead", "repair_lead_audit"}
            and not lead_audit_writer_enabled()):
        _finish_action(conn, action_id, CampaignActionState.FAILED,
                       error="Grant Salesforce audit records feature flag disabled")
        return ActionExecution(
            CampaignActionState.FAILED,
            "Salesforce Notes and Activities are disabled; nothing was submitted.")
    try:
        if row["action_type"] == "create_campaign":
            return _confirm_campaign_create(conn, gateway, row)
        if row["action_type"] == "add_campaign_members":
            return _confirm_membership(conn, gateway, row)
        if row["action_type"] == "create_person_lead":
            from . import salesforce_record_actions as records
            return records.confirm_person_lead(conn, gateway, row)
        if row["action_type"] == "create_opportunity":
            from . import salesforce_record_actions as records
            return records.confirm_opportunity(conn, gateway, row)
        if row["action_type"] == "enrich_existing_lead":
            from . import salesforce_record_actions as records
            return records.confirm_lead_enrichment(conn, gateway, row)
        if row["action_type"] == "repair_lead_audit":
            from . import salesforce_record_actions as records
            return records.confirm_lead_audit_repair(conn, gateway, row)
        raise ValueError("unknown Salesforce action type")
    except SalesforceCompositeRolledBack as exc:
        _finish_action(
            conn, action_id, CampaignActionState.FAILED,
            error=f"SalesforceCompositeRolledBack: {str(exc)[:1000]}")
        return ActionExecution(
            CampaignActionState.FAILED,
            "Salesforce rejected the all-or-none action; nothing was changed.",
        )
    except requests.Timeout as exc:
        _finish_action(conn, action_id, CampaignActionState.UNKNOWN,
                       error=f"{type(exc).__name__}: reconciliation required")
        return ActionExecution(
            CampaignActionState.UNKNOWN,
            "Salesforce timed out after submission. The result is unknown; Grant will not retry "
            "until a human reconciles Salesforce.", unknown=1,
        )
    except (requests.RequestException, ValueError, KeyError) as exc:
        current = _load_action(conn, action_id)
        if bool(current["external_write_started"]):
            detail = str(exc).strip()[:240]
            _finish_action(conn, action_id, CampaignActionState.UNKNOWN,
                           error=(f"{type(exc).__name__}: {detail}; reconciliation required"
                                  if detail else
                                  f"{type(exc).__name__}: reconciliation required"))
            return ActionExecution(
                CampaignActionState.UNKNOWN,
                "A Salesforce write had started before a later error. The outcome "
                "requires reconciliation; Grant will not retry it automatically.",
                campaign_id=str(current["campaign_id"] or ""), unknown=1,
            )
        _finish_action(conn, action_id, CampaignActionState.FAILED,
                       error=f"{type(exc).__name__}: {str(exc)[:300]}")
        return ActionExecution(
            CampaignActionState.FAILED,
            f"Salesforce rejected the action ({type(exc).__name__}); nothing was submitted.",
        )


def _confirm_campaign_create(conn: sqlite3.Connection,
                             gateway: SalesforceCampaignGateway,
                             row: sqlite3.Row) -> ActionExecution:
    """Create one Campaign and read it back before reporting success."""
    payload = json.loads(str(row["payload_json"]))
    _mark_external_write_started(conn, str(row["id"]))
    result = gateway.create_campaign(dict(payload["campaign"]))
    if not result.success:
        error = result.error or "Salesforce returned no Campaign ID"
        _finish_action(conn, str(row["id"]), CampaignActionState.FAILED, error=error)
        return ActionExecution(CampaignActionState.FAILED,
                               f"Campaign creation failed: {error}")
    if not result.record_id:
        _finish_action(conn, str(row["id"]), CampaignActionState.UNKNOWN,
                       error="Salesforce reported success without a Campaign ID")
        return ActionExecution(
            CampaignActionState.UNKNOWN,
            "Salesforce reported success without a Campaign ID; reconciliation is required.",
            unknown=1,
        )
    with conn:
        conn.execute("UPDATE crm_actions SET campaign_id=? WHERE id=?",
                     (result.record_id, row["id"]))
    campaign = gateway.get_record("Campaign", result.record_id)
    _finish_action(conn, str(row["id"]), CampaignActionState.COMPLETE,
                   campaign_id=campaign.record_id)
    return ActionExecution(
        CampaignActionState.COMPLETE,
        f"Created Salesforce Campaign {campaign.name}: {campaign.link}",
        campaign_id=campaign.record_id,
    )


def _confirm_membership(conn: sqlite3.Connection,
                        gateway: SalesforceCampaignGateway,
                        row: sqlite3.Row) -> ActionExecution:
    """Create approved Leads/status/members and report every partial outcome."""
    campaign_id = validate_record_id(str(row["campaign_id"]), "Campaign")
    item_rows = list(conn.execute(
        "SELECT * FROM crm_action_items WHERE action_id=? ORDER BY id", (row["id"],)
    ))
    if not gateway.member_status_exists(campaign_id):
        _mark_external_write_started(conn, str(row["id"]))
        status_result = gateway.create_member_status(campaign_id)
        if not status_result.success:
            error = status_result.error or "member status creation failed"
            _finish_action(conn, str(row["id"]), CampaignActionState.FAILED, error=error)
            return ActionExecution(CampaignActionState.FAILED,
                                   f"No members were added because {MEMBER_STATUS} could not be created.")

    record_ids: dict[int, str] = {}
    create_rows: list[sqlite3.Row] = []
    create_payloads: list[dict[str, object]] = []
    unresolved = 0
    for item in item_rows:
        proposed = json.loads(str(item["proposed_json"]))
        if item["operation"] == "existing_record":
            record_ids[int(item["id"])] = str(proposed["salesforce_ref"]["record_id"])
        elif item["operation"] == "create_org_lead":
            create_rows.append(item)
            create_payloads.append(dict(proposed["proposed_lead"]))
        else:
            unresolved += 1
            with conn:
                conn.execute("UPDATE crm_action_items SET state='unresolved' WHERE id=?",
                             (item["id"],))

    failed = 0
    if create_payloads:
        _mark_external_write_started(conn, str(row["id"]))
        lead_results = gateway.create_leads(create_payloads)
        if len(lead_results) != len(create_rows):
            raise ValueError("Salesforce Lead result count did not match request")
        for item, result in zip(create_rows, lead_results):
            with conn:
                if result.success and result.record_id:
                    record_ids[int(item["id"])] = result.record_id
                    conn.execute(
                        "UPDATE crm_action_items SET state='lead_created',salesforce_id=? WHERE id=?",
                        (result.record_id, item["id"]),
                    )
                else:
                    failed += 1
                    conn.execute(
                        "UPDATE crm_action_items SET state='failed',error=? WHERE id=?",
                        (result.error or "Lead create failed", item["id"]),
                    )

    existing = gateway.existing_members(campaign_id, list(record_ids.values()))
    already = sum(record_id in existing for record_id in record_ids.values())
    member_rows: list[int] = []
    member_payloads: list[dict[str, object]] = []
    for item_id, record_id in record_ids.items():
        if record_id in existing:
            with conn:
                conn.execute("UPDATE crm_action_items SET state='already_present' WHERE id=?",
                             (item_id,))
            continue
        field = "LeadId" if record_id.startswith("00Q") else "ContactId"
        member_rows.append(item_id)
        member_payloads.append({
            "CampaignId": campaign_id, field: record_id, "Status": MEMBER_STATUS,
        })
    added = 0
    if member_payloads:
        _mark_external_write_started(conn, str(row["id"]))
        member_results = gateway.create_members(member_payloads)
        if len(member_results) != len(member_rows):
            raise ValueError("Salesforce CampaignMember result count did not match request")
        for item_id, result in zip(member_rows, member_results):
            with conn:
                if result.success and result.record_id:
                    added += 1
                    conn.execute(
                        """UPDATE crm_action_items
                           SET state='added',campaign_member_id=? WHERE id=?""",
                        (result.record_id, item_id),
                    )
                else:
                    failed += 1
                    conn.execute(
                        "UPDATE crm_action_items SET state='failed',error=? WHERE id=?",
                        (result.error or "CampaignMember create failed", item_id),
                    )
    state = (CampaignActionState.COMPLETE
             if failed == 0 and unresolved == 0 else CampaignActionState.PARTIAL)
    _finish_action(conn, str(row["id"]), state, campaign_id=campaign_id,
                   error=f"{failed} item failures" if failed else "")
    message = (
        f"Salesforce campaign update: {added} added, {already} already present, "
        f"{unresolved} unresolved, {failed} failed."
    )
    return ActionExecution(state, message, campaign_id=campaign_id, added=added,
                           already_present=already, unresolved=unresolved, failed=failed)
