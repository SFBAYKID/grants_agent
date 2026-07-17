"""Audited create-only workflow that turns a Grant contact into Salesforce records.

Given a lead with a verified (or LinkedIn-only) contact, this module prepares an
immutable preview of one fully-populated person Lead plus one activity Task — or,
when the organization already exists in Salesforce as a single high-confidence
match, only the Task attached to the existing record with no duplicate Lead.
It reuses the campaign preview/nonce/confirm machinery unchanged, so every write
is requester-bound, TTL-limited, hash-verified, and create-only.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date

from . import salesforce
from .salesforce_campaign_gateway import (
    SalesforceCampaignGateway,
    SalesforceRecordRef,
    validate_record_id,
)
from .salesforce_campaign_models import (
    ActionExecution,
    CampaignActionState,
    MemberPlan,
    PreparedAction,
)
from .salesforce_campaign_ownership import requester_owner
from .salesforce_campaign_policy import validate_action_context
from .salesforce_campaigns import (
    _finish_action as finish_action,
    _mark_external_write_started as mark_external_write_started,
    _store_action as store_action,
)

ACTION_TYPE = "create_contact_record"
# Contact statuses that may back a Salesforce record. Website-verified contacts
# carry a verbatim on-page email; linkedin_only rows carry a profile URL and no
# email, and every rendering must say the profile's ownership is unverified.
_USABLE_CONTACT_STATUSES = ("verified", "linkedin_only")


def split_person_name(name: str) -> tuple[str, str]:
    """Split a contact's full name into (FirstName, LastName); never guess.

    A single token becomes the LastName with a blank FirstName — Salesforce
    requires LastName and Grant does not invent given names."""
    tokens = str(name or "").split()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return " ".join(tokens[:-1]), tokens[-1]


def _amount_text(row: sqlite3.Row) -> str:
    """Render the award amount without ever inventing one."""
    amount = row["amount"]
    if amount is None or float(amount) <= 0:
        return "amount not recorded"
    return f"${float(amount):,.0f}"


def _grant_summary(row: sqlite3.Row) -> str:
    """One honest sentence describing the grant behind this record."""
    program = str(row["program"] or "unlabeled program")
    window = f"{row['funds_start'] or 'unknown'} to {row['funds_end'] or 'unknown'}"
    source = str(row["detail_url"] or "not provided")
    return (
        f"{_amount_text(row)} {program} grant; spend window {window}. "
        f"Grant source {source}."
    )


def _contact_evidence(contact: sqlite3.Row) -> str:
    """Describe where the contact came from, honestly per evidence kind."""
    source = str(contact["source_url"] or "unknown source")
    if str(contact["contact_status"]) == "linkedin_only":
        return f"Evidence is a LinkedIn profile (ownership not verified): {source}."
    return f"Contact verified verbatim on {source}."


def contact_lead_payload(
    lead: sqlite3.Row,
    contact: sqlite3.Row,
    requester: str,
    action_id: str,
    owner: SalesforceRecordRef,
) -> dict[str, object]:
    """Build the person Lead create payload from evidenced fields only.

    Keys are omitted entirely when no evidence exists; Street, PostalCode, and
    Industry are never set because Grant has no verified source for them."""
    validate_record_id(owner.record_id, "User")
    entity = str(lead["entity_name"] or "").strip()
    first, last = split_person_name(str(contact["name"] or ""))
    payload: dict[str, object] = {
        "LastName": last or entity,
        "Company": entity,
        "OwnerId": owner.record_id,
        "Status": "New",
        "LeadSource": "Other",
        "Description": (
            f"{_grant_summary(lead)} {_contact_evidence(contact)} "
            "Created by Grant from a public contact. "
            f"Grant lead {lead['id']}; action {action_id}; "
            f"requested by Slack user {requester}."
        ),
    }
    from .. import db

    if first:
        payload["FirstName"] = first
    title = str(contact["title"] or "")
    if title and (
        db.canonical_entity_key(title).partition("|")[0]
        != db.canonical_entity_key(entity).partition("|")[0]
    ):
        # A "title" that is just the organization name is not a person's role.
        payload["Title"] = title
    if contact["email"]:
        payload["Email"] = str(contact["email"])
    if contact["phone"]:
        payload["Phone"] = str(contact["phone"])
    if lead["state"]:
        payload["State"] = str(lead["state"])
    if lead["location_city"]:
        payload["City"] = str(lead["location_city"])
    if contact["official_domain"]:
        payload["Website"] = str(contact["official_domain"])
    return payload


def grant_task_payload(
    lead: sqlite3.Row,
    contact: sqlite3.Row,
    requester: str,
    action_id: str,
    owner: SalesforceRecordRef,
    today: str,
) -> dict[str, object]:
    """Build the activity Task payload; WhoId/WhatId is attached by mode."""
    from .. import db

    validate_record_id(owner.record_id, "User")
    title = str(contact["title"] or "")
    if not title or (
        db.canonical_entity_key(title).partition("|")[0]
        == db.canonical_entity_key(str(lead["entity_name"] or "")).partition("|")[0]
    ):
        title = "title not verified"
    return {
        "Subject": "Grant AI: record created from grant lead",
        "ActivityDate": today,
        "OwnerId": owner.record_id,
        "Description": (
            f"Grant found {contact['name']} ({title}) at "
            f"{lead['entity_name']}. Grant identified {_grant_summary(lead)} "
            f"{_contact_evidence(contact)} "
            f"Grant lead {lead['id']}; action {action_id}; "
            f"requested by Slack user {requester}."
        ),
    }


def _resolve_existing_record(
    lead: sqlite3.Row,
    lookup: Callable[..., salesforce.SFResult],
) -> SalesforceRecordRef | None:
    """Decide new-Lead vs attach-to-existing; fail closed on every unprovable case."""
    entity = str(lead["entity_name"] or "")
    state = str(lead["state"] or "")
    result = lookup(entity, "", "", state)
    status = result.status
    if status == salesforce.SFResultStatus.NO_MATCH:
        return None
    if status != salesforce.SFResultStatus.FOUND:
        raise ValueError(
            f"Salesforce duplicate check was {status.value}; refusing to create "
            "anything until a human resolves it"
        )
    highs = [
        m
        for m in result.matches
        if m.confidence == "high" and m.sobject in ("Lead", "Contact", "Account")
    ]
    if len(highs) != 1:
        links = ", ".join(m.link for m in result.matches[:3] if m.link)
        raise ValueError(
            "Salesforce shows more than one plausible existing record for "
            f"{entity}; refusing to pick one automatically. Candidates: "
            f"{links or 'no links returned'}"
        )
    match = highs[0]
    ref = SalesforceRecordRef(
        sobject=match.sobject,
        record_id=match.record_id,
        name=match.name,
        link=match.link,
        company=match.company or match.name,
        state=match.state,
    )
    from .salesforce_campaign_policy import record_matches_organization

    if not record_matches_organization(ref, entity, state):
        raise ValueError(
            f"The existing Salesforce record ({match.name}) does not provably "
            f"belong to {entity}; refusing to attach anything to it"
        )
    validate_record_id(ref.record_id, ref.sobject)
    return ref


def _select_contact(
    contacts: list[sqlite3.Row], contact_id: int | None
) -> sqlite3.Row:
    """Choose the evidence-backed contact, failing closed on any ambiguity."""
    usable = [
        c for c in contacts if str(c["contact_status"]) in _USABLE_CONTACT_STATUSES
    ]
    if not usable:
        raise ValueError(
            "no verified or LinkedIn-sourced contact exists for this lead; "
            "run find_contact (or a LinkedIn search bound to the lead) first"
        )
    if contact_id is not None:
        for c in usable:
            if int(c["id"]) == int(contact_id):
                return c
        raise ValueError(
            f"contact {contact_id} is not a usable contact for this lead"
        )
    verified = [c for c in usable if str(c["contact_status"]) == "verified"]
    pool = verified or usable
    if len(pool) > 1:
        names = ", ".join(f"#{c['id']} {c['name']}" for c in pool)
        raise ValueError(
            f"several contacts are on file ({names}); specify contact_id"
        )
    return pool[0]


def _rerun_guard(conn: sqlite3.Connection, lead_id: int) -> None:
    """Refuse a second contact-record action for the same lead."""
    row = conn.execute(
        """SELECT a.id, a.state FROM crm_action_items i
           JOIN crm_actions a ON a.id = i.action_id
           WHERE i.lead_id=? AND a.action_type=?
             AND a.state IN ('ready','committing','complete','partial','unknown')
           ORDER BY a.created_at DESC LIMIT 1""",
        (lead_id, ACTION_TYPE),
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"a Salesforce contact record for this lead already exists or is "
            f"pending (action {row['id']}, state {row['state']}); cancel or "
            "reconcile it before creating another"
        )


def _blank_disclosures(payload: dict[str, object], mode: str) -> list[str]:
    """Name every absent field so the preview never implies unknown data."""
    notes: list[str] = []
    if mode == "new_lead":
        labels = {
            "FirstName": "FirstName: blank — single-word name; Grant never invents one",
            "Title": "Title: blank — not verified",
            "Email": "Email: blank — no verified email (LinkedIn-only evidence)",
            "Phone": "Phone: blank — not verified",
            "State": "State: blank — not on file",
            "City": "City: blank — no NCES district-office city on file",
            "Website": "Website: blank — no verified official domain",
        }
        notes.extend(text for key, text in labels.items() if key not in payload)
        notes.append(
            "Street, PostalCode, Industry: blank — Grant has no verified source "
            "for these and never guesses."
        )
    return notes


def _preview_text(
    mode: str,
    lead: sqlite3.Row,
    contact: sqlite3.Row,
    owner: SalesforceRecordRef,
    owner_email: str,
    lead_payload: dict[str, object] | None,
    task_payload: dict[str, object],
    target: SalesforceRecordRef | None,
) -> str:
    """Render the exact, complete preview a rep approves."""
    lines: list[str] = []
    entity = str(lead["entity_name"] or "")
    if mode == "new_lead" and lead_payload is not None:
        lines.append(
            f"Create Salesforce person Lead for *{contact['name']}* — "
            f"{entity} ({lead['state'] or 'state unknown'})"
        )
        shown = [
            ("FirstName", "FirstName"),
            ("LastName", "LastName"),
            ("Title", "Title"),
            ("Email", "Email"),
            ("Phone", "Phone"),
            ("Company", "Company"),
            ("State", "State"),
            ("City", "City (NCES district office)"),
            ("Website", "Website"),
        ]
        for key, label in shown:
            if key in lead_payload:
                lines.append(f"• {label}: {lead_payload[key]}")
        lines.append(f"• Owner: {owner.name} ({owner_email})")
        lines.append(f"• Description: {lead_payload['Description']}")
        lines.extend(f"• {note}" for note in _blank_disclosures(lead_payload, mode))
        lines.append(
            "Duplicate check: a complete Salesforce search found no existing "
            "record for this organization."
        )
    else:
        assert target is not None  # attach mode always carries a target
        lines.append(
            f"*{entity}* is already in Salesforce — no duplicate Lead will be "
            "created."
        )
        lines.append(
            f"• Existing record: {target.sobject} \"{target.name}\" — "
            f"{target.link or 'no link'} (single high-confidence match)"
        )
        lines.append("• One activity Task will be attached to it instead.")
    lines.append("Plus one activity Task:")
    lines.append(f"• Subject: {task_payload['Subject']}")
    lines.append(
        f"• Date: {task_payload['ActivityDate']}  • Owner: {owner.name}"
    )
    lines.append(f"• Notes: {task_payload['Description']}")
    return "\n".join(lines)


def prepare_contact_record(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    lead_id: int,
    contact_id: int | None = None,
    lookup: Callable[..., salesforce.SFResult] = salesforce.lookup,
) -> PreparedAction:
    """Prepare (never execute) an immutable contact-record preview."""
    from .. import db

    validate_action_context(workspace, channel, thread_ts, requester)
    lead = conn.execute(
        "SELECT * FROM leads WHERE id=?", (int(lead_id),)
    ).fetchone()
    if lead is None:
        raise ValueError(f"unknown or stale Grant lead {lead_id}")
    _rerun_guard(conn, int(lead_id))
    contact = _select_contact(db.contacts_for_lead(conn, int(lead_id)), contact_id)
    owner, owner_email = requester_owner(gateway, requester)
    target = _resolve_existing_record(lead, lookup)
    action_seed = str(uuid.uuid4())
    today = date.today().isoformat()
    mode = "attach_existing" if target is not None else "new_lead"
    lead_payload = (
        None
        if target is not None
        else contact_lead_payload(lead, contact, requester, action_seed, owner)
    )
    task_payload = grant_task_payload(
        lead, contact, requester, action_seed, owner, today
    )
    if target is not None:
        key = "WhoId" if target.sobject in ("Lead", "Contact") else "WhatId"
        task_payload[key] = target.record_id
    payload: dict[str, object] = {
        "mode": mode,
        "lead_id": int(lead_id),
        "contact_id": int(contact["id"]),
        "contact_status": str(contact["contact_status"]),
        "lead": lead_payload,
        "task": task_payload,
        "target": asdict(target) if target is not None else None,
        "owner": {
            "salesforce_user_id": owner.record_id,
            "name": owner.name,
            "email": owner_email,
        },
    }
    preview = _preview_text(
        mode, lead, contact, owner, owner_email, lead_payload, task_payload, target
    )
    plan = MemberPlan(
        lead_id=int(lead_id),
        canonical_entity_key=str(lead["canonical_entity_key"] or ""),
        entity_name=str(lead["entity_name"] or ""),
        state=str(lead["state"] or ""),
        operation=(
            "attach_task_existing" if target is not None else "create_contact_lead"
        ),
        salesforce_ref=target,
        proposed_lead=lead_payload,
        note=f"contact #{contact['id']} {contact['name']}",
    )
    action_id, nonce, expires = store_action(
        conn,
        ACTION_TYPE,
        workspace,
        channel,
        thread_ts,
        requester,
        payload,
        plans=[plan],
        action_id=action_seed,
    )
    return PreparedAction(action_id, nonce, preview, expires)


def _set_item_state(
    conn: sqlite3.Connection, action_id: str, state: str, salesforce_id: str = ""
) -> None:
    """Record the per-lead item outcome for the audit trail."""
    with conn:
        conn.execute(
            "UPDATE crm_action_items SET state=?, salesforce_id=COALESCE(?,salesforce_id) "
            "WHERE action_id=?",
            (state, salesforce_id or None, action_id),
        )


def confirm_contact_record(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    row: sqlite3.Row,
) -> ActionExecution:
    """Execute a confirmed contact-record action; called from confirm_action.

    Timeout and post-write errors are handled by the confirm_action wrapper
    (UNKNOWN when a write may have reached Salesforce)."""
    import json

    payload = json.loads(str(row["payload_json"]))
    action_id = str(row["id"])
    task_payload = dict(payload["task"])
    if payload["mode"] == "attach_existing":
        target = payload["target"] or {}
        who_what = task_payload.get("WhoId") or task_payload.get("WhatId") or ""
        validate_record_id(str(who_what), str(target.get("sobject", "Lead")))
        mark_external_write_started(conn, action_id)
        result = gateway.create_task(task_payload)
        if not result.success:
            finish_action(
                conn,
                action_id,
                CampaignActionState.FAILED,
                error=f"Task create failed: {result.error}",
            )
            _set_item_state(conn, action_id, "failed")
            return ActionExecution(
                CampaignActionState.FAILED,
                f"Salesforce rejected the activity Task ({result.error}); "
                "nothing was created.",
                failed=1,
            )
        if not result.record_id:
            finish_action(
                conn,
                action_id,
                CampaignActionState.UNKNOWN,
                error="Task create returned no id",
            )
            return ActionExecution(
                CampaignActionState.UNKNOWN,
                "Salesforce accepted the Task but returned no id; "
                "reconciliation required.",
                unknown=1,
            )
        _set_item_state(conn, action_id, "added", result.record_id)
        finish_action(conn, action_id, CampaignActionState.COMPLETE)
        link = str(target.get("link") or "the existing record")
        return ActionExecution(
            CampaignActionState.COMPLETE,
            f"Logged the Grant activity Task on {link}; no duplicate Lead was "
            "created.",
            added=1,
        )
    # new_lead mode: Lead first, then Task attached to it.
    mark_external_write_started(conn, action_id)
    lead_result = gateway.create_lead(dict(payload["lead"]))
    if not lead_result.success:
        finish_action(
            conn,
            action_id,
            CampaignActionState.FAILED,
            error=f"Lead create failed: {lead_result.error}",
        )
        _set_item_state(conn, action_id, "failed")
        return ActionExecution(
            CampaignActionState.FAILED,
            f"Salesforce rejected the Lead ({lead_result.error}); nothing was "
            "created.",
            failed=1,
        )
    if not lead_result.record_id:
        finish_action(
            conn,
            action_id,
            CampaignActionState.UNKNOWN,
            error="Lead create returned no id",
        )
        return ActionExecution(
            CampaignActionState.UNKNOWN,
            "Salesforce accepted the Lead but returned no id; reconciliation "
            "required.",
            unknown=1,
        )
    validate_record_id(lead_result.record_id, "Lead")
    _set_item_state(conn, action_id, "lead_created", lead_result.record_id)
    with conn:
        conn.execute(
            "UPDATE crm_actions SET campaign_id=?, updated_at=datetime('now') "
            "WHERE id=?",
            (lead_result.record_id, action_id),
        )
    record = gateway.get_record("Lead", lead_result.record_id)
    task_payload["WhoId"] = lead_result.record_id
    task_result = gateway.create_task(task_payload)
    lead_name = record.name or str(payload["lead"].get("LastName", "Lead"))
    if not task_result.success:
        finish_action(
            conn,
            action_id,
            CampaignActionState.PARTIAL,
            campaign_id=lead_result.record_id,
            error=f"Task create failed: {task_result.error}",
        )
        return ActionExecution(
            CampaignActionState.PARTIAL,
            f"Lead {lead_name} was created (id {lead_result.record_id}) but the "
            f"activity Task failed: {task_result.error}. The Lead is real — add "
            "the note manually; Grant will not retry automatically.",
            added=1,
            failed=1,
        )
    _set_item_state(conn, action_id, "added", lead_result.record_id)
    finish_action(
        conn,
        action_id,
        CampaignActionState.COMPLETE,
        campaign_id=lead_result.record_id,
    )
    return ActionExecution(
        CampaignActionState.COMPLETE,
        f"Created Salesforce Lead {lead_name} (id {lead_result.record_id}) and "
        "logged the Grant activity Task.",
        campaign_id=lead_result.record_id,
        added=1,
    )
