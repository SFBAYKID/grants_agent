"""Audited Salesforce Campaign approval and persistence workflow.

Natural-language intent may prepare these actions, but only immutable, requester-bound
Slack confirmations can execute the separate create-only Salesforce gateway.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta

import requests

from .. import db
from .salesforce_campaign_models import (
    ActionExecution,
    CampaignActionState,
    CampaignDraft,
    MemberPlan,
    PreparedAction,
)
from .salesforce_campaign_ownership import (
    organization_lead_payload,
    requester_owner,
)
from .salesforce_campaign_policy import (
    iso_timestamp as _iso,
    now_utc as _now,
    record_matches_organization as _record_matches_organization,
    validate_action_context as _validate_context,
    write_channel_allowed,
    writer_enabled,
)
from .salesforce_campaign_gateway import (
    MAX_ACTION_ORGANIZATIONS,
    MEMBER_STATUS,
    SalesforceCampaignGateway,
    SalesforceRecordRef,
    parse_record_link,
    validate_record_id,
)

# 24 hours: a rep reads Slack on their own schedule — a 15-minute
# window meant every real-world tap landed on a corpse (Chase hit
# this live, 2026-07-18). Freshness is still guarded by the payload
# hash and the create-only writer.
ACTION_TTL_MINUTES = 24 * 60


def _stable_json(value: object) -> str:
    """Serialize an immutable preview deterministically for payload hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: str) -> str:
    """Hash nonces and immutable payloads before persistence."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _store_action(
    conn: sqlite3.Connection,
    action_type: str,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    payload: dict[str, object],
    campaign_id: str = "",
    plans: list[MemberPlan] | None = None,
    action_id: str | None = None,
) -> tuple[str, str, str]:
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
            "salesforce_ref": asdict(plan.salesforce_ref)
            if plan.salesforce_ref
            else None,
            "proposed_lead": plan.proposed_lead,
            "note": plan.note,
        }
        stored_plans.append(
            {
                "lead_id": plan.lead_id,
                "canonical_entity_key": plan.canonical_entity_key,
                "operation": plan.operation,
                "proposed": proposed,
            }
        )
    items_hash = _hash(_stable_json(stored_plans))
    with conn:
        conn.execute(
            """INSERT INTO crm_actions
                 (id,action_type,workspace,channel,thread_ts,requested_by,state,
                  payload_json,payload_hash,items_hash,nonce_hash,expires_at,
                  campaign_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                action_id,
                action_type,
                workspace,
                channel,
                thread_ts,
                requester,
                CampaignActionState.READY.value,
                payload_json,
                _hash(payload_json),
                items_hash,
                _hash(nonce),
                _iso(expires),
                campaign_id or None,
                _iso(now),
                _iso(now),
            ),
        )
        for plan, stored in zip(plans or [], stored_plans):
            conn.execute(
                """INSERT INTO crm_action_items
                     (action_id,lead_id,canonical_entity_key,operation,proposed_json,
                      state,verification_state)
                   VALUES (?,?,?,?,?,'ready','pending')""",
                (
                    action_id,
                    plan.lead_id,
                    plan.canonical_entity_key,
                    plan.operation,
                    _stable_json(stored["proposed"]),
                ),
            )
    return action_id, nonce, _iso(expires)


def prepare_campaign_creation(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    draft: CampaignDraft,
) -> PreparedAction:
    """Validate and persist a new-Campaign preview without writing Salesforce."""
    _validate_context(workspace, channel, thread_ts, requester)
    if not draft.name.strip() or len(draft.name.strip()) > 80:
        raise ValueError("Campaign name must be between 1 and 80 characters")
    now = _iso(_now())
    with conn:
        conn.execute(
            """UPDATE crm_actions SET state=?,updated_at=?
               WHERE action_type='create_campaign' AND workspace=? AND channel=?
                 AND thread_ts=? AND requested_by=? AND state=? AND expires_at<=?""",
            (
                CampaignActionState.EXPIRED.value,
                now,
                workspace,
                channel,
                thread_ts,
                requester,
                CampaignActionState.READY.value,
                now,
            ),
        )
    existing = conn.execute(
        """SELECT id FROM crm_actions
           WHERE action_type='create_campaign' AND workspace=? AND channel=?
             AND thread_ts=? AND requested_by=? AND state=?
           LIMIT 1""",
        (
            workspace,
            channel,
            thread_ts,
            requester,
            CampaignActionState.READY.value,
        ),
    ).fetchone()
    if existing is not None:
        raise ValueError(
            "An active Campaign creation preview already exists in this thread; "
            "cancel it before preparing another"
        )
    if draft.owner_id:
        validate_record_id(draft.owner_id, "User")
    types, statuses = gateway.campaign_picklists()
    if draft.campaign_type not in types:
        raise ValueError(f"Campaign Type '{draft.campaign_type}' is not active")
    if draft.status not in statuses:
        raise ValueError(f"Campaign Status '{draft.status}' is not active")
    action_seed = str(uuid.uuid4())
    payload = draft.payload(action_seed, requester)
    try:
        action_id, nonce, expires = _store_action(
            conn,
            "create_campaign",
            workspace,
            channel,
            thread_ts,
            requester,
            {
                "campaign": payload,
                "owner_label": draft.owner_label,
                "provenance_seed": action_seed,
            },
            action_id=action_seed,
        )
    except sqlite3.IntegrityError as exc:
        if "ux_crm_one_ready_campaign_creation" not in str(exc):
            raise
        raise ValueError(
            "An active Campaign creation preview already exists in this thread; "
            "cancel it before preparing another"
        ) from exc
    preview = (
        f"Create Salesforce Campaign *{payload['Name']}*\n"
        f"• Type: {payload['Type']}\n• Status: {payload['Status']}\n"
        f"• Active: {payload['IsActive']}\n• Owner: {draft.owner_label}\n"
        f"• Member status later: {MEMBER_STATUS} (not responded)\n"
        "No Leads or Campaign Members will be added in this step."
    )
    return PreparedAction(action_id, nonce, preview, expires)


def prepare_membership(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    campaign: SalesforceRecordRef,
    lead_ids: list[int],
    supplied_links: dict[int, str] | None = None,
    resolved_records: dict[int, SalesforceRecordRef] | None = None,
    canonical_keys: dict[int, str] | None = None,
    allow_org_leads: bool = False,
    allow_resolved_only: bool = False,
) -> PreparedAction:
    """Resolve a frozen Grant lead set and persist the exact membership preview."""
    _validate_context(workspace, channel, thread_ts, requester)
    validate_record_id(campaign.record_id, "Campaign")
    unique_ids = list(dict.fromkeys(int(item) for item in lead_ids))
    if not unique_ids or len(unique_ids) > MAX_ACTION_ORGANIZATIONS:
        raise ValueError("Choose between 1 and 200 Grant leads")
    placeholders = ",".join("?" for _ in unique_ids)
    rows = list(
        conn.execute(
            f"SELECT * FROM leads WHERE id IN ({placeholders}) ORDER BY id",
            unique_ids,
        )
    )
    if len(rows) != len(unique_ids):
        raise ValueError("One or more Grant lead IDs are stale or unknown")
    action_seed = str(uuid.uuid4())
    supplied_links = supplied_links or {}
    resolved_records = resolved_records or {}
    canonical_keys = canonical_keys or {}
    plans_by_key: dict[str, MemberPlan] = {}
    organization_owner: SalesforceRecordRef | None = None
    organization_owner_email = ""
    for row in rows:
        key = str(
            canonical_keys.get(int(row["id"]))
            or row["canonical_entity_key"]
            or db.canonical_entity_key(str(row["entity_name"]), str(row["state"] or ""))
        )
        if key in plans_by_key:
            continue
        supplied = supplied_links.get(int(row["id"]))
        frozen_record = resolved_records.get(int(row["id"]))
        supplied_mismatch = False
        candidates: list[SalesforceRecordRef]
        if supplied:
            sobject, record_id = parse_record_link(supplied, {"Lead", "Contact"})
            supplied_record = gateway.get_record(sobject, record_id)
            if _record_matches_organization(
                supplied_record, str(row["entity_name"]), str(row["state"] or "")
            ):
                candidates = [supplied_record]
            else:
                candidates = []
                supplied_mismatch = True
        elif frozen_record is not None:
            if _record_matches_organization(
                frozen_record, str(row["entity_name"]), str(row["state"] or "")
            ):
                candidates = [frozen_record]
            else:
                candidates = []
                supplied_mismatch = True
        else:
            candidates = [
                candidate
                for candidate in gateway.find_people(
                    str(row["entity_name"]), str(row["state"] or "")
                )
                if _record_matches_organization(
                    candidate, str(row["entity_name"]), str(row["state"] or "")
                )
            ]
        account_finder = getattr(gateway, "find_accounts", None)
        accounts = (
            [
                account
                for account in account_finder(
                    str(row["entity_name"]), str(row["state"] or "")
                )
                if _record_matches_organization(
                    account, str(row["entity_name"]), str(row["state"] or "")
                )
            ]
            if callable(account_finder) and not candidates and not supplied_mismatch
            else []
        )
        if len(candidates) == 1:
            plan = MemberPlan(
                int(row["id"]),
                key,
                str(row["entity_name"]),
                str(row["state"] or ""),
                "existing_record",
                salesforce_ref=candidates[0],
            )
        elif len(candidates) > 1:
            plan = MemberPlan(
                int(row["id"]),
                key,
                str(row["entity_name"]),
                str(row["state"] or ""),
                "ambiguous",
                note="Multiple Salesforce Leads/Contacts require selection.",
            )
        elif supplied_mismatch:
            plan = MemberPlan(
                int(row["id"]),
                key,
                str(row["entity_name"]),
                str(row["state"] or ""),
                "unresolved",
                note="Supplied Salesforce record does not match this organization/state.",
            )
        elif accounts:
            plan = MemberPlan(
                int(row["id"]),
                key,
                str(row["entity_name"]),
                str(row["state"] or ""),
                "unresolved",
                note=(
                    "An exact Salesforce Account exists, but a Lead/Contact is required "
                    "for Campaign membership."
                ),
            )
        elif allow_org_leads:
            if organization_owner is None:
                organization_owner, organization_owner_email = requester_owner(
                    gateway, requester
                )
            plan = MemberPlan(
                int(row["id"]),
                key,
                str(row["entity_name"]),
                str(row["state"] or ""),
                "create_org_lead",
                proposed_lead=organization_lead_payload(
                    row, requester, action_seed, organization_owner
                ),
                note=(
                    "No individual contact verified; organization name fills Company "
                    f"and LastName; owner is {organization_owner.name}."
                ),
            )
        else:
            plan = MemberPlan(
                int(row["id"]),
                key,
                str(row["entity_name"]),
                str(row["state"] or ""),
                "unresolved",
                note="Provide a Salesforce Lead/Contact link or approve an organization-only Lead.",
            )
        plans_by_key[key] = plan
    plans = list(plans_by_key.values())
    excluded = [plan for plan in plans if plan.operation in {"unresolved", "ambiguous"}]
    if excluded and not allow_resolved_only:
        raise ValueError(
            f"{len(excluded)} of {len(plans)} organizations are unresolved or "
            "ambiguous. No approval was created; resolve them or explicitly choose "
            "the resolved-only subset"
        )
    if allow_resolved_only:
        plans = [
            plan for plan in plans if plan.operation not in {"unresolved", "ambiguous"}
        ]
    payload = {
        "campaign": asdict(campaign),
        "lead_ids": [plan.lead_id for plan in plans],
        "allow_org_leads": allow_org_leads,
        "allow_resolved_only": allow_resolved_only,
        "excluded_organization_count": len(excluded),
        "excluded_organizations": [
            {
                "entity_name": plan.entity_name,
                "state": plan.state,
                "reason": plan.note,
            }
            for plan in excluded
        ],
        "member_status": MEMBER_STATUS,
        "provenance_seed": action_seed,
        "organization_lead_owner": (
            {
                "salesforce_user_id": organization_owner.record_id,
                "name": organization_owner.name,
                "email": organization_owner_email,
            }
            if organization_owner is not None
            else None
        ),
    }
    existing = sum(plan.operation == "existing_record" for plan in plans)
    creating = sum(plan.operation == "create_org_lead" for plan in plans)
    unresolved = sum(plan.operation in {"unresolved", "ambiguous"} for plan in plans)
    if existing + creating == 0:
        raise ValueError(
            "No organizations can be added yet; resolve a Salesforce Lead/Contact "
            "or approve organization-only Leads before confirming"
        )
    action_id, nonce, expires = _store_action(
        conn,
        "add_campaign_members",
        workspace,
        channel,
        thread_ts,
        requester,
        payload,
        campaign.record_id,
        plans,
        action_id=action_seed,
    )
    mapping_lines: list[str] = []
    for plan in plans:
        label = f"{plan.entity_name} ({plan.state or '?'})"
        if plan.operation == "existing_record" and plan.salesforce_ref is not None:
            mapping_lines.append(
                f"• {label} → {plan.salesforce_ref.sobject} "
                f"{plan.salesforce_ref.name}: {plan.salesforce_ref.link}"
            )
        elif plan.operation == "create_org_lead":
            mapping_lines.append(
                f"• {label} → create organization-only Lead; no person fields"
            )
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
            f"\n• New Lead owner: {organization_owner.name} "
            f"({organization_owner_email})"
        )
    if excluded:
        excluded_lines = "\n".join(
            f"  - {plan.entity_name} ({plan.state or '?'}): {plan.note}"
            for plan in excluded
        )
        preview += (
            "\n• Explicitly excluded unresolved/ambiguous organizations: "
            f"{len(excluded)}\n{excluded_lines}"
        )
    return PreparedAction(action_id, nonce, preview, expires)


def _load_action(conn: sqlite3.Connection, action_id: str) -> sqlite3.Row:
    """Load one durable action or raise a safe stale-action error."""
    row = conn.execute("SELECT * FROM crm_actions WHERE id=?", (action_id,)).fetchone()
    if row is None:
        raise ValueError("Salesforce action was not found")
    return row


def _authorize_action(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    nonce: str,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    require_ready: bool = True,
) -> None:
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
    if require_ready and row["state"] != CampaignActionState.READY.value:
        raise ValueError(f"Salesforce action is already {row['state']}")
    if require_ready and datetime.fromisoformat(str(row["expires_at"])) <= _now():
        raise TimeoutError("Salesforce approval preview expired")
    if _hash(str(row["payload_json"])) != row["payload_hash"]:
        raise ValueError("Salesforce approval payload changed after preview")
    stored_items = [
        {
            "lead_id": item["lead_id"],
            "canonical_entity_key": item["canonical_entity_key"],
            "operation": item["operation"],
            "proposed": json.loads(str(item["proposed_json"])),
        }
        for item in conn.execute(
            """SELECT lead_id,canonical_entity_key,operation,proposed_json
             FROM crm_action_items WHERE action_id=? ORDER BY id""",
            (row["id"],),
        )
    ]
    if _hash(_stable_json(stored_items)) != row["items_hash"]:
        raise ValueError("Salesforce approval item mapping changed after preview")


def cancel_action(conn: sqlite3.Connection, action_id: str, requester: str) -> bool:
    """Cancel a ready action only when requested by its initiating user."""
    with conn:
        cur = conn.execute(
            """UPDATE crm_actions SET state=?,updated_at=?
               WHERE id=? AND requested_by=? AND state=?""",
            (
                CampaignActionState.CANCELLED.value,
                _iso(_now()),
                action_id,
                requester,
                CampaignActionState.READY.value,
            ),
        )
    return cur.rowcount == 1


def record_approval_attempt(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    actor: str,
    workspace: str,
    channel: str,
    thread_ts: str,
    action_ts: str,
    outcome: str,
    reason: str = "",
) -> None:
    """Persist a secret-free, idempotent audit row for one Slack button attempt."""
    row = conn.execute(
        "SELECT batch_id FROM crm_actions WHERE id=?", (action_id,)
    ).fetchone()
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO crm_campaign_approval_attempts
                 (id,action_id,batch_id,actor_slack,workspace,channel,thread_ts,
                  action_ts,outcome,reason,occurred_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                action_id or None,
                str(row["batch_id"] or "") if row else None,
                actor,
                workspace,
                channel,
                thread_ts,
                action_ts,
                outcome,
                reason[:240] or None,
                _iso(_now()),
            ),
        )


def stored_action_result(
    conn: sqlite3.Connection,
    action_id: str,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
) -> ActionExecution:
    """Return a prior action's persisted result for safe repeated button clicks."""
    row = _load_action(conn, action_id)
    if (
        row["workspace"] != workspace
        or row["channel"] != channel
        or row["thread_ts"] != thread_ts
        or row["requested_by"] != requester
    ):
        raise PermissionError("Salesforce action does not belong to this user/context")
    try:
        state = CampaignActionState(str(row["state"]))
    except ValueError:
        state = CampaignActionState.UNKNOWN
    counts = {
        str(item[0]): int(item[1])
        for item in conn.execute(
            "SELECT state,COUNT(*) FROM crm_action_items WHERE action_id=? GROUP BY state",
            (action_id,),
        )
    }
    added = counts.get("added", 0)
    already = counts.get("already_present", 0)
    unresolved = counts.get("unresolved", 0)
    failed = counts.get("failed", 0)
    unknown = counts.get("unknown", 0) + counts.get("verification_pending", 0)
    if row["action_type"] == "create_campaign":
        payload = json.loads(str(row["payload_json"]))
        name = str((payload.get("campaign") or {}).get("Name") or "Campaign")
        message = f"Salesforce Campaign {name} is already {state.value}" + (
            f" (ID {row['campaign_id']})." if row["campaign_id"] else "."
        )
    else:
        message = (
            f"This Salesforce action is already {state.value}: {added} added, "
            f"{already} already present, {unresolved} unresolved, {failed} failed, "
            f"{unknown} unknown."
        )
    return ActionExecution(
        state,
        message,
        campaign_id=str(row["campaign_id"] or ""),
        added=added,
        already_present=already,
        unresolved=unresolved,
        failed=failed,
        unknown=unknown,
    )


def _begin_commit(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Compare-and-set READY to COMMITTING so retries/double-clicks cannot duplicate."""
    with conn:
        cur = conn.execute(
            """UPDATE crm_actions SET state=?,approved_at=?,updated_at=?,attempts=attempts+1
               WHERE id=? AND state=?""",
            (
                CampaignActionState.COMMITTING.value,
                _iso(_now()),
                _iso(_now()),
                row["id"],
                CampaignActionState.READY.value,
            ),
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


def _finish_action(
    conn: sqlite3.Connection,
    action_id: str,
    state: CampaignActionState,
    campaign_id: str = "",
    error: str = "",
) -> None:
    """Persist the terminal/unknown outcome without deleting audit history."""
    with conn:
        conn.execute(
            """UPDATE crm_actions SET state=?,campaign_id=COALESCE(?,campaign_id),
                      last_error=?,committed_at=?,updated_at=? WHERE id=?""",
            (
                state.value,
                campaign_id or None,
                error or None,
                _iso(_now()),
                _iso(_now()),
                action_id,
            ),
        )


def confirm_action(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action_id: str,
    nonce: str,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    dry_run: bool = False,
) -> ActionExecution:
    """Execute a stored create-only action after all approval gates pass."""
    row = _load_action(conn, action_id)
    if row["action_type"] == "add_campaign_members" and row["state"] in {
        CampaignActionState.UNKNOWN.value,
        CampaignActionState.COMMITTING.value,
    }:
        _authorize_action(
            conn,
            row,
            nonce,
            workspace,
            channel,
            thread_ts,
            requester,
            require_ready=False,
        )
        from .salesforce_campaign_execution import reconcile_membership

        return reconcile_membership(conn, gateway, row)
    if row["action_type"] == "create_campaign" and row["state"] in {
        CampaignActionState.UNKNOWN.value,
        CampaignActionState.COMMITTING.value,
    }:
        _authorize_action(
            conn,
            row,
            nonce,
            workspace,
            channel,
            thread_ts,
            requester,
            require_ready=False,
        )
        from .salesforce_campaign_execution import reconcile_campaign_creation

        return reconcile_campaign_creation(conn, gateway, row)
    try:
        _authorize_action(conn, row, nonce, workspace, channel, thread_ts, requester)
    except TimeoutError:
        _finish_action(conn, action_id, CampaignActionState.EXPIRED)
        raise
    _begin_commit(conn, row)
    if dry_run:
        _finish_action(conn, action_id, CampaignActionState.DRY_RUN)
        return ActionExecution(
            CampaignActionState.DRY_RUN,
            "Dry run verified the approval; Salesforce was not written.",
        )
    if not writer_enabled():
        _finish_action(
            conn,
            action_id,
            CampaignActionState.FAILED,
            error="campaign writes feature flag disabled",
        )
        return ActionExecution(
            CampaignActionState.FAILED,
            "Salesforce campaign writes are disabled; nothing was created.",
        )
    try:
        if row["action_type"] == "create_campaign":
            from .salesforce_campaign_execution import execute_campaign_creation

            return execute_campaign_creation(conn, gateway, row)
        if row["action_type"] == "add_campaign_members":
            from .salesforce_campaign_execution import execute_membership

            return execute_membership(conn, gateway, row)
        if row["action_type"] == "create_contact_record":
            # Function-local import: contact records reuse this module's store/
            # authorize machinery, so a top-level import would be circular.
            from .salesforce_contact_records import confirm_contact_record

            return confirm_contact_record(conn, gateway, row)
        raise ValueError("unknown Salesforce action type")
    except PermissionError as exc:
        # verify_write_scope fails closed BEFORE any create POST (bad/missing
        # write config, org mismatch, non-sandbox), so nothing was written even
        # though a commit was begun — resolve cleanly to FAILED, never stranded.
        _finish_action(
            conn,
            action_id,
            CampaignActionState.FAILED,
            error=f"write scope refused: {str(exc)[:200]}",
        )
        return ActionExecution(
            CampaignActionState.FAILED,
            f"Salesforce was not changed: {str(exc)[:200]}",
        )
    except requests.Timeout as exc:
        _finish_action(
            conn,
            action_id,
            CampaignActionState.UNKNOWN,
            error=f"{type(exc).__name__}: reconciliation required",
        )
        return ActionExecution(
            CampaignActionState.UNKNOWN,
            "Salesforce timed out after submission. The result is unknown; Grant will not retry "
            "until a human reconciles Salesforce.",
            unknown=1,
        )
    except (requests.RequestException, ValueError, KeyError) as exc:
        current = _load_action(conn, action_id)
        if bool(current["external_write_started"]):
            _finish_action(
                conn,
                action_id,
                CampaignActionState.UNKNOWN,
                error=f"{type(exc).__name__}: reconciliation required",
            )
            return ActionExecution(
                CampaignActionState.UNKNOWN,
                "A Salesforce write had started before a later error. The outcome "
                "requires reconciliation; Grant will not retry it automatically.",
                campaign_id=str(current["campaign_id"] or ""),
                unknown=1,
            )
        _finish_action(
            conn,
            action_id,
            CampaignActionState.FAILED,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
        return ActionExecution(
            CampaignActionState.FAILED,
            f"Salesforce rejected the action ({type(exc).__name__}); nothing was submitted.",
        )
