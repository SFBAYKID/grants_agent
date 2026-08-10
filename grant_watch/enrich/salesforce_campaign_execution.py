"""Verified, idempotent execution for Salesforce Campaign membership actions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

import requests

from .salesforce_campaign_gateway import (
    MEMBER_STATUS,
    CreateResult,
    SalesforceCampaignGateway,
    validate_record_id,
)
from .salesforce_campaign_models import ActionExecution, CampaignActionState
from .salesforce_campaign_policy import iso_timestamp, now_utc


def _stable_json(value: object) -> str:
    """Serialize one exact outbound request for durable hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: object) -> str:
    """Hash one exact outbound Salesforce request."""
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _delays(gateway: SalesforceCampaignGateway) -> tuple[float, ...]:
    """Return the gateway's bounded eventual-consistency retry schedule."""
    values = tuple(getattr(gateway, "reconciliation_delays", (0.0,)))
    return values or (0.0,)


def _begin_attempt(
    conn: sqlite3.Connection,
    action_id: str,
    operation: str,
    payloads: list[dict[str, object]],
    submitted_ids: list[str],
) -> str:
    """Persist in-flight state before any potentially mutating HTTP request."""
    attempt_id = str(uuid.uuid4())
    now = iso_timestamp(now_utc())
    with conn:
        conn.execute(
            """INSERT INTO crm_campaign_write_attempts
                 (id,action_id,operation,request_hash,state,submitted_ids_json,
                  started_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                attempt_id,
                action_id,
                operation,
                _hash(payloads),
                "in_flight",
                _stable_json(submitted_ids),
                now,
            ),
        )
        conn.execute(
            "UPDATE crm_actions SET external_write_started=1,updated_at=? WHERE id=?",
            (now, action_id),
        )
    return attempt_id


def _finish_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    state: str,
    results: list[CreateResult],
    error: str = "",
) -> None:
    """Persist Salesforce response IDs before any subsequent readback."""
    with conn:
        conn.execute(
            """UPDATE crm_campaign_write_attempts
               SET state=?,returned_ids_json=?,error=?,finished_at=? WHERE id=?""",
            (
                state,
                _stable_json([result.record_id for result in results]),
                error or None,
                iso_timestamp(now_utc()),
                attempt_id,
            ),
        )


def execute_campaign_creation(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action: sqlite3.Row,
) -> ActionExecution:
    """Create one Campaign and report success only after exact record readback."""
    action_id = str(action["id"])
    payload = json.loads(str(action["payload_json"]))
    campaign_payload = dict(payload["campaign"])
    attempt_id = _begin_attempt(
        conn,
        action_id,
        "create_campaign",
        [campaign_payload],
        [str(campaign_payload.get("Name") or "")],
    )
    result = gateway.create_campaign(campaign_payload)
    _finish_attempt(
        conn,
        attempt_id,
        "response_received",
        [result],
        result.error,
    )
    if not result.success:
        with conn:
            conn.execute(
                """UPDATE crm_actions
                   SET state='failed',last_error=?,committed_at=?,updated_at=? WHERE id=?""",
                (
                    result.error or "Campaign creation failed",
                    iso_timestamp(now_utc()),
                    iso_timestamp(now_utc()),
                    action_id,
                ),
            )
        return ActionExecution(
            CampaignActionState.FAILED,
            f"Campaign creation failed: {result.error or 'Salesforce rejected it'}",
        )
    if not result.record_id:
        raise ValueError("Salesforce reported Campaign success without an ID")
    with conn:
        conn.execute(
            "UPDATE crm_actions SET campaign_id=?,updated_at=? WHERE id=?",
            (result.record_id, iso_timestamp(now_utc()), action_id),
        )
    campaign = None
    for delay in _delays(gateway):
        if delay:
            time.sleep(delay)
        try:
            campaign = gateway.get_record("Campaign", result.record_id)
            break
        except (requests.RequestException, ValueError, KeyError):
            continue
    if campaign is None:
        raise requests.Timeout("Campaign is not visible after creation")
    expected_name = str(campaign_payload.get("Name") or "")
    if campaign.name != expected_name:
        raise ValueError("Campaign readback did not match the approved name")
    with conn:
        conn.execute(
            """UPDATE crm_actions
               SET state='complete',campaign_id=?,committed_at=?,updated_at=?
               WHERE id=?""",
            (
                campaign.record_id,
                iso_timestamp(now_utc()),
                iso_timestamp(now_utc()),
                action_id,
            ),
        )
    return ActionExecution(
        CampaignActionState.COMPLETE,
        f"Created Salesforce Campaign {campaign.name}: {campaign.link}",
        campaign_id=campaign.record_id,
    )


def reconcile_campaign_creation(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action: sqlite3.Row,
) -> ActionExecution:
    """Reconcile a Campaign create by returned ID without issuing another POST."""
    attempt = conn.execute(
        """SELECT returned_ids_json FROM crm_campaign_write_attempts
           WHERE action_id=? AND operation='create_campaign'
           ORDER BY started_at DESC LIMIT 1""",
        (action["id"],),
    ).fetchone()
    returned_ids = (
        json.loads(str(attempt["returned_ids_json"]))
        if attempt and attempt["returned_ids_json"]
        else []
    )
    campaign_id = str(returned_ids[0] or "") if returned_ids else ""
    campaign = None
    if campaign_id:
        validate_record_id(campaign_id, "Campaign")
        for delay in _delays(gateway):
            if delay:
                time.sleep(delay)
            try:
                campaign = gateway.get_record("Campaign", campaign_id)
                break
            except (requests.RequestException, ValueError, KeyError):
                continue
    if campaign is None:
        with conn:
            conn.execute(
                """UPDATE crm_actions SET state='unknown',last_error=?,
                          committed_at=?,updated_at=? WHERE id=?""",
                (
                    "Campaign create requires manual reconciliation",
                    iso_timestamp(now_utc()),
                    iso_timestamp(now_utc()),
                    action["id"],
                ),
            )
        return ActionExecution(
            CampaignActionState.UNKNOWN,
            "Salesforce Campaign creation remains unknown; Grant did not retry it.",
            unknown=1,
        )
    payload = json.loads(str(action["payload_json"]))
    expected_name = str((payload.get("campaign") or {}).get("Name") or "")
    if campaign.name != expected_name:
        raise ValueError("Campaign readback did not match the approved name")
    with conn:
        conn.execute(
            """UPDATE crm_actions SET state='complete',campaign_id=?,
                      committed_at=?,updated_at=?,last_error=NULL WHERE id=?""",
            (
                campaign.record_id,
                iso_timestamp(now_utc()),
                iso_timestamp(now_utc()),
                action["id"],
            ),
        )
    return ActionExecution(
        CampaignActionState.COMPLETE,
        f"Verified Salesforce Campaign {campaign.name}: {campaign.link}",
        campaign_id=campaign.record_id,
    )


def _verify_frozen_scope(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action: sqlite3.Row,
) -> None:
    """Recheck the exact writer org frozen by a batch before its first create."""
    batch_id = str(action["batch_id"] or "")
    if not batch_id:
        return
    frozen = conn.execute(
        """SELECT writer_org_id,writer_is_sandbox,writer_host
           FROM crm_campaign_batches WHERE id=?""",
        (batch_id,),
    ).fetchone()
    if frozen is None:
        raise PermissionError("Campaign batch writer identity is missing")
    target_id = str(action["batch_target_id"] or "")
    target = conn.execute(
        """SELECT expected_source_row_count,stored_source_row_count,unique_org_count,
                  selection_hash,approved_org_count,approved_selection_hash,
                  completion_mode,campaign_id,action_id
           FROM crm_campaign_batch_targets WHERE id=? AND batch_id=?""",
        (target_id, batch_id),
    ).fetchone()
    if target is None:
        raise PermissionError("Campaign batch target is missing")
    manifest_rows = list(
        conn.execute(
            """SELECT bi.canonical_entity_key,bi.representative_lead_id,
                      bi.source_lead_ids_json,bi.grades_json,bi.entity_name,
                      bi.state_code,bi.resolution_state,bi.salesforce_sobject,
                      bi.salesforce_id,bi.salesforce_account_id,bi.note,bi.item_hash,
                      bi.crm_action_item_id,ai.action_id AS mapped_action_id,
                      ai.canonical_entity_key AS mapped_canonical_entity_key
               FROM crm_campaign_batch_items bi
               LEFT JOIN crm_action_items ai ON ai.id=bi.crm_action_item_id
               WHERE bi.target_id=? ORDER BY bi.canonical_entity_key""",
            (target_id,),
        )
    )
    manifest_items = len(manifest_rows)
    manifest_payloads: list[dict[str, object]] = []
    approved_manifest_keys: list[str] = []
    for item in manifest_rows:
        payload = {
            "canonical_entity_key": item["canonical_entity_key"],
            "representative_lead_id": item["representative_lead_id"],
            "source_lead_ids": json.loads(str(item["source_lead_ids_json"])),
            "grades": json.loads(str(item["grades_json"])),
            "entity_name": item["entity_name"],
            "state": item["state_code"],
            "resolution_state": item["resolution_state"],
            "salesforce_sobject": str(item["salesforce_sobject"] or ""),
            "salesforce_id": str(item["salesforce_id"] or ""),
            "salesforce_account_id": str(item["salesforce_account_id"] or ""),
            "note": str(item["note"] or ""),
        }
        if _hash(payload) != item["item_hash"]:
            raise PermissionError("Campaign batch item hash does not match")
        manifest_payloads.append(payload)
        included = (
            str(target["completion_mode"]) == "full"
            or item["resolution_state"] == "existing_record"
        )
        if included:
            approved_manifest_keys.append(str(item["canonical_entity_key"]))
            if (
                item["crm_action_item_id"] is None
                or item["mapped_action_id"] != action["id"]
                or item["mapped_canonical_entity_key"] != item["canonical_entity_key"]
            ):
                raise PermissionError(
                    "Campaign batch item is not mapped one-to-one to its action"
                )
        elif item["crm_action_item_id"] is not None:
            raise PermissionError(
                "Excluded Campaign batch item is unexpectedly mapped to an action"
            )
    action_keys = [
        str(row[0])
        for row in conn.execute(
            """SELECT canonical_entity_key FROM crm_action_items
               WHERE action_id=? ORDER BY canonical_entity_key""",
            (action["id"],),
        )
    ]
    approved_items = int(target["approved_org_count"])
    action_items = len(action_keys)
    exact_approved_subset = (
        action_items == approved_items
        and action_keys == approved_manifest_keys
        and _hash(action_keys) == target["approved_selection_hash"]
    )
    if (
        target["campaign_id"] != action["campaign_id"]
        or target["action_id"] != action["id"]
        or int(target["expected_source_row_count"])
        != int(target["stored_source_row_count"])
        or manifest_items != int(target["unique_org_count"])
        or not exact_approved_subset
        or (
            str(target["completion_mode"]) == "full"
            and approved_items != manifest_items
        )
        or _hash(manifest_payloads) != target["selection_hash"]
    ):
        raise PermissionError("Campaign batch completeness evidence does not match")
    current = gateway.verify_write_scope()
    current_host = current.instance_url.split("://", 1)[-1].split("/", 1)[0].lower()
    if (
        current.organization_id != frozen["writer_org_id"]
        or int(current.is_sandbox) != int(frozen["writer_is_sandbox"])
        or current_host != str(frozen["writer_host"])
    ):
        raise PermissionError("Salesforce writer org changed after the batch preview")


def _read_members(
    gateway: SalesforceCampaignGateway,
    campaign_id: str,
    record_ids: list[str],
) -> dict[str, tuple[str, str]]:
    """Read exact CampaignMember records, including limited legacy fake support."""
    reader = getattr(gateway, "member_records", None)
    if callable(reader):
        return dict(reader(campaign_id, record_ids))
    # Older offline test doubles implemented only the former set-returning API.
    return {
        record_id: ("", MEMBER_STATUS)
        for record_id in gateway.existing_members(campaign_id, record_ids)
    }


def _poll_members(
    gateway: SalesforceCampaignGateway,
    campaign_id: str,
    record_ids: list[str],
) -> dict[str, tuple[str, str]]:
    """Perform bounded read-only reconciliation for Salesforce query visibility lag."""
    latest: dict[str, tuple[str, str]] = {}
    for delay in _delays(gateway):
        if delay:
            time.sleep(delay)
        try:
            latest = _read_members(gateway, campaign_id, record_ids)
        except (requests.RequestException, ValueError, KeyError):
            latest = {}
        if all(record_id in latest for record_id in record_ids):
            break
    return latest


def _mark_verified_member(
    conn: sqlite3.Connection,
    item_id: int,
    state: str,
    campaign_member_id: str,
) -> None:
    """Persist a member outcome only after exact Salesforce readback."""
    with conn:
        conn.execute(
            """UPDATE crm_action_items
               SET state=?,campaign_member_id=COALESCE(NULLIF(?,''),campaign_member_id),
                   verification_state='verified',verified_at=?,error=NULL
               WHERE id=?""",
            (
                state,
                campaign_member_id,
                iso_timestamp(now_utc()),
                item_id,
            ),
        )


def _store_record_ids(conn: sqlite3.Connection, record_ids: dict[int, str]) -> None:
    """Freeze resolved Salesforce IDs before CampaignMember creation begins."""
    with conn:
        for item_id, record_id in record_ids.items():
            conn.execute(
                """UPDATE crm_action_items
                   SET salesforce_id=?,verification_state='pending' WHERE id=?""",
                (record_id, item_id),
            )


def _close_reconciled_attempts(conn: sqlite3.Connection, action_id: str) -> None:
    """Close indeterminate member writes after every submitted target is visible."""
    with conn:
        conn.execute(
            """UPDATE crm_campaign_write_attempts
               SET state='reconciled_present',finished_at=?,error=NULL
               WHERE action_id=? AND operation='create_campaign_members'
                 AND state='in_flight'""",
            (iso_timestamp(now_utc()), action_id),
        )


def _create_organization_leads(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action_id: str,
    rows: list[sqlite3.Row],
) -> tuple[dict[int, str], int]:
    """Create and read back approved organization-only Leads."""
    if not rows:
        return {}, 0
    payloads = [
        dict(json.loads(str(row["proposed_json"]))["proposed_lead"]) for row in rows
    ]
    attempt_id = _begin_attempt(
        conn,
        action_id,
        "create_organization_leads",
        payloads,
        [str(row["canonical_entity_key"]) for row in rows],
    )
    results = gateway.create_leads(payloads)
    if len(results) != len(rows):
        raise ValueError("Salesforce Lead result count did not match request")
    _finish_attempt(conn, attempt_id, "response_received", results)
    created: dict[int, str] = {}
    failed = 0
    for row, payload, result in zip(rows, payloads, results):
        item_id = int(row["id"])
        if not result.success or not result.record_id:
            failed += 1
            with conn:
                conn.execute(
                    """UPDATE crm_action_items
                       SET state='failed',verification_state='verified_failure',error=?
                       WHERE id=?""",
                    (result.error or "Lead create failed", item_id),
                )
            continue
        record = None
        for delay in _delays(gateway):
            if delay:
                time.sleep(delay)
            try:
                record = gateway.get_record("Lead", result.record_id)
                break
            except (requests.RequestException, ValueError, KeyError):
                continue
        expected_company = str(payload["Company"])
        expected_state = str(payload.get("State") or "")
        if record is None or (
            record.company != expected_company
            or record.state.upper() != expected_state.upper()
        ):
            with conn:
                conn.execute(
                    """UPDATE crm_action_items
                       SET state='unknown',verification_state='unknown',
                           salesforce_id=?,error='Lead readback mismatch' WHERE id=?""",
                    (result.record_id, item_id),
                )
            continue
        created[item_id] = result.record_id
        with conn:
            conn.execute(
                """UPDATE crm_action_items
                   SET state='lead_created',verification_state='lead_verified',
                       salesforce_id=?,verified_at=? WHERE id=?""",
                (
                    result.record_id,
                    iso_timestamp(now_utc()),
                    item_id,
                ),
            )
    return created, failed


def _ensure_member_status(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action_id: str,
    campaign_id: str,
) -> str:
    """Create and verify the disclosed non-response status when absent."""
    if gateway.member_status_exists(campaign_id):
        return ""
    payload = {
        "CampaignId": campaign_id,
        "Label": MEMBER_STATUS,
        "HasResponded": False,
    }
    attempt_id = _begin_attempt(
        conn, action_id, "create_member_status", [payload], [campaign_id]
    )
    result = gateway.create_member_status(campaign_id)
    _finish_attempt(conn, attempt_id, "response_received", [result], result.error)
    if not result.success:
        return result.error or "member status creation failed"
    visible = False
    for delay in _delays(gateway):
        if delay:
            time.sleep(delay)
        try:
            visible = gateway.member_status_exists(campaign_id)
        except (requests.RequestException, ValueError, KeyError):
            visible = False
        if visible:
            break
    if not visible:
        raise requests.Timeout("Campaign member status is not visible after creation")
    return ""


def _result_counts(
    conn: sqlite3.Connection, action_id: str
) -> tuple[int, int, int, int, int]:
    """Return verified added/present plus unresolved, failed, and unknown counts."""
    rows = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """SELECT state,COUNT(*) FROM crm_action_items
               WHERE action_id=? GROUP BY state""",
            (action_id,),
        )
    }
    return (
        rows.get("added", 0),
        rows.get("already_present", 0),
        rows.get("unresolved", 0),
        rows.get("failed", 0),
        rows.get("unknown", 0) + rows.get("verification_pending", 0),
    )


def _excluded_organizations(conn: sqlite3.Connection, action_id: str) -> list[str]:
    """Return exact human-excluded organizations from immutable action evidence."""
    row = conn.execute(
        "SELECT payload_json,batch_target_id FROM crm_actions WHERE id=?",
        (action_id,),
    ).fetchone()
    if row is None:
        return []
    target_id = str(row["batch_target_id"] or "")
    if target_id:
        return [
            f"{item['entity_name']} ({item['state_code']})"
            for item in conn.execute(
                """SELECT entity_name,state_code
                   FROM crm_campaign_batch_items
                   WHERE target_id=? AND resolution_state!='existing_record'
                   ORDER BY canonical_entity_key""",
                (target_id,),
            )
        ]
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError):
        return []
    values = payload.get("excluded_organizations") or []
    if not isinstance(values, list):
        return []
    labels: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        name = str(value.get("entity_name") or "").strip()
        state = str(value.get("state") or "").strip()
        if name:
            labels.append(f"{name} ({state or '?'})")
    return labels


def _campaign_link(gateway: object | None, campaign_id: str) -> str:
    """The Campaign's Lightning URL, or "" if it cannot be built.

    Best-effort by design: a link is an aid, and failing to construct one must never
    turn a successful, verified write into an error the rep has to interpret.
    """
    builder = getattr(gateway, "lightning_link", None)
    if builder is None or not campaign_id:
        return ""
    try:
        return str(builder("Campaign", campaign_id) or "")
    except Exception:  # noqa: BLE001 — a missing link never fails a completed write
        return ""


def _finish_membership(
    conn: sqlite3.Connection,
    action_id: str,
    campaign_id: str,
    gateway: object | None = None,
) -> ActionExecution:
    """Derive and persist an honest action state from verified item outcomes."""
    added, already, unresolved, failed, unknown = _result_counts(conn, action_id)
    excluded = _excluded_organizations(conn, action_id)
    state = (
        CampaignActionState.UNKNOWN
        if unknown
        else CampaignActionState.PARTIAL
        if failed
        else CampaignActionState.COMPLETE
    )
    with conn:
        conn.execute(
            """UPDATE crm_actions
               SET state=?,campaign_id=?,last_error=?,committed_at=?,updated_at=?
               WHERE id=?""",
            (
                state.value,
                campaign_id,
                f"{unknown} item outcomes unknown" if unknown else None,
                iso_timestamp(now_utc()),
                iso_timestamp(now_utc()),
                action_id,
            ),
        )
        target_id = conn.execute(
            """SELECT a.batch_target_id,t.completion_mode
               FROM crm_actions a
               LEFT JOIN crm_campaign_batch_targets t ON t.id=a.batch_target_id
               WHERE a.id=?""",
            (action_id,),
        ).fetchone()
        if target_id and target_id["batch_target_id"]:
            target_key = str(target_id["batch_target_id"])
            target_state = (
                "partial_by_user"
                if state is CampaignActionState.COMPLETE
                and target_id["completion_mode"] == "partial_by_user"
                else state.value
            )
            conn.execute(
                """UPDATE crm_campaign_batch_targets SET state=?,updated_at=?
                   WHERE id=?""",
                (target_state, iso_timestamp(now_utc()), target_key),
            )
            batch = conn.execute(
                """SELECT t.batch_id,b.completion_mode
                   FROM crm_campaign_batch_targets t
                   JOIN crm_campaign_batches b ON b.id=t.batch_id
                   WHERE t.id=?""",
                (target_key,),
            ).fetchone()
            if batch:
                batch_id = str(batch["batch_id"])
                states = {
                    str(item[0])
                    for item in conn.execute(
                        """SELECT state FROM crm_campaign_batch_targets
                           WHERE batch_id=?""",
                        (batch_id,),
                    )
                }
                if (
                    states <= {"complete", "partial_by_user"}
                    and batch["completion_mode"] == "partial_by_user"
                ):
                    parent_state = "partial_by_user"
                elif states <= {"complete"}:
                    parent_state = "complete"
                elif "unknown" in states:
                    parent_state = "unknown"
                elif states & {"partial", "failed"}:
                    parent_state = "partial"
                else:
                    parent_state = "in_progress"
                conn.execute(
                    """UPDATE crm_campaign_batches SET state=?,updated_at=? WHERE id=?""",
                    (parent_state, iso_timestamp(now_utc()), batch_id),
                )
    message = (
        f"Salesforce campaign verification: {added} added, {already} already present, "
        f"{unresolved} unresolved, {failed} failed, {unknown} unknown."
    )
    # ALWAYS HAND BACK THE LINK. Chase had to ask "give me the link" after 13 leads
    # were added — the create confirmation carried one and this did not, so the
    # thread ended with the rep going to look for the thing Grant had just written.
    # A confirmation that reports work without a way to see it is half a message.
    link = _campaign_link(gateway, campaign_id)
    if link:
        message += f"\nCampaign: {link}"
    if excluded:
        message += (
            f" Explicitly excluded/skipped before approval: {len(excluded)} — "
            + ", ".join(excluded)
            + "."
        )
    return ActionExecution(
        state,
        message,
        campaign_id=campaign_id,
        added=added,
        already_present=already,
        unresolved=unresolved,
        failed=failed,
        unknown=unknown,
    )


def execute_membership(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action: sqlite3.Row,
) -> ActionExecution:
    """Execute one fully resolved membership action and verify every member by GET."""
    action_id = str(action["id"])
    campaign_id = validate_record_id(str(action["campaign_id"]), "Campaign")
    _verify_frozen_scope(conn, gateway, action)
    status_error = _ensure_member_status(conn, gateway, action_id, campaign_id)
    if status_error:
        with conn:
            conn.execute(
                """UPDATE crm_actions SET state='failed',last_error=?,committed_at=?,
                          updated_at=? WHERE id=?""",
                (
                    status_error,
                    iso_timestamp(now_utc()),
                    iso_timestamp(now_utc()),
                    action_id,
                ),
            )
        return ActionExecution(
            CampaignActionState.FAILED,
            f"No members were added because {MEMBER_STATUS} could not be created.",
        )
    item_rows = list(
        conn.execute(
            "SELECT * FROM crm_action_items WHERE action_id=? ORDER BY id", (action_id,)
        )
    )
    if any(
        row["operation"] not in {"existing_record", "create_org_lead"}
        for row in item_rows
    ):
        raise ValueError("Unresolved organizations cannot reach Salesforce execution")
    record_ids: dict[int, str] = {}
    create_rows: list[sqlite3.Row] = []
    for item in item_rows:
        proposed: dict[str, Any] = json.loads(str(item["proposed_json"]))
        if item["operation"] == "existing_record":
            record_ids[int(item["id"])] = str(proposed["salesforce_ref"]["record_id"])
        else:
            create_rows.append(item)
    created, _lead_failures = _create_organization_leads(
        conn, gateway, action_id, create_rows
    )
    record_ids.update(created)
    _store_record_ids(conn, record_ids)
    before = _read_members(gateway, campaign_id, list(record_ids.values()))
    pending_rows: list[tuple[int, str]] = []
    for item_id, record_id in record_ids.items():
        if record_id in before:
            _mark_verified_member(
                conn, item_id, "already_present", before[record_id][0]
            )
        else:
            pending_rows.append((item_id, record_id))
    if pending_rows:
        payloads = [
            {
                "CampaignId": campaign_id,
                "LeadId" if record_id.startswith("00Q") else "ContactId": record_id,
                "Status": MEMBER_STATUS,
            }
            for _item_id, record_id in pending_rows
        ]
        attempt_id = _begin_attempt(
            conn,
            action_id,
            "create_campaign_members",
            payloads,
            [record_id for _item_id, record_id in pending_rows],
        )
        results = gateway.create_members(payloads)
        if len(results) != len(pending_rows):
            raise ValueError(
                "Salesforce CampaignMember result count did not match request"
            )
        _finish_attempt(conn, attempt_id, "response_received", results)
        with conn:
            for (item_id, _record_id), result in zip(pending_rows, results):
                conn.execute(
                    """UPDATE crm_action_items
                       SET state='verification_pending',verification_state='pending',
                           campaign_member_id=?,error=? WHERE id=?""",
                    (
                        result.record_id or None,
                        result.error or None,
                        item_id,
                    ),
                )
        after = _poll_members(
            gateway, campaign_id, [record_id for _item_id, record_id in pending_rows]
        )
        for (item_id, record_id), result in zip(pending_rows, results):
            visible = after.get(record_id)
            if visible and visible[1] == MEMBER_STATUS:
                if result.success and visible[0] != result.record_id:
                    with conn:
                        conn.execute(
                            """UPDATE crm_action_items
                               SET state='unknown',verification_state='unknown',
                                   error='CampaignMember readback ID mismatch'
                               WHERE id=?""",
                            (item_id,),
                        )
                else:
                    state = "added" if result.success else "already_present"
                    _mark_verified_member(conn, item_id, state, visible[0])
            elif result.success:
                with conn:
                    conn.execute(
                        """UPDATE crm_action_items
                           SET state='unknown',verification_state='unknown',
                               error='CampaignMember not visible after create' WHERE id=?""",
                        (item_id,),
                    )
            else:
                with conn:
                    conn.execute(
                        """UPDATE crm_action_items
                           SET state='failed',verification_state='verified_failure'
                           WHERE id=?""",
                        (item_id,),
                    )
    return _finish_membership(conn, action_id, campaign_id, gateway)


def reconcile_membership(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    action: sqlite3.Row,
) -> ActionExecution:
    """Read Salesforce only to reconcile an indeterminate membership action."""
    _verify_frozen_scope(conn, gateway, action)
    campaign_id = validate_record_id(str(action["campaign_id"]), "Campaign")
    item_rows = list(
        conn.execute(
            """SELECT id,salesforce_id,state,campaign_member_id FROM crm_action_items
               WHERE action_id=? ORDER BY id""",
            (action["id"],),
        )
    )
    ids = [str(row["salesforce_id"]) for row in item_rows if row["salesforce_id"]]
    visible = _poll_members(gateway, campaign_id, ids) if ids else {}
    for item in item_rows:
        record_id = str(item["salesforce_id"] or "")
        if (
            record_id
            and record_id in visible
            and visible[record_id][1] == MEMBER_STATUS
        ):
            returned_member_id = str(item["campaign_member_id"] or "")
            if returned_member_id and returned_member_id != visible[record_id][0]:
                with conn:
                    conn.execute(
                        """UPDATE crm_action_items
                           SET state='unknown',verification_state='unknown',
                               error='CampaignMember readback ID mismatch'
                           WHERE id=?""",
                        (item["id"],),
                    )
            else:
                state = "added" if returned_member_id else "already_present"
                _mark_verified_member(
                    conn, int(item["id"]), state, visible[record_id][0]
                )
        elif str(item["state"]) not in {"failed", "already_present", "added"}:
            with conn:
                conn.execute(
                    """UPDATE crm_action_items
                       SET state='unknown',verification_state='unknown' WHERE id=?""",
                    (item["id"],),
                )
    result = _finish_membership(conn, str(action["id"]), campaign_id, gateway)
    if result.state is not CampaignActionState.UNKNOWN:
        _close_reconciled_attempts(conn, str(action["id"]))
    return result
