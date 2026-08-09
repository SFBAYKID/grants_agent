"""Exact multi-state Salesforce Campaign selection and approval preparation.

The model may describe a batch, but this module selects the rows server-side,
freezes their counts and hashes, resolves Salesforce identities, and creates one
isolated child approval per Campaign only when every included organization is safe.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import defaultdict
from typing import Protocol
from urllib.parse import urlparse

from .. import db
from .salesforce_campaign_batch_models import (
    CampaignTargetRequest,
    PreparedCampaignBatch,
)
from .salesforce_campaign_gateway import (
    MAX_ACTION_ORGANIZATIONS,
    SalesforceCampaignGateway,
    SalesforceOrganizationIdentity,
    SalesforceRecordRef,
    parse_record_link,
)
from .salesforce_campaign_models import PreparedAction
from .salesforce_campaign_policy import (
    iso_timestamp,
    now_utc,
    record_matches_organization,
    validate_action_context,
)
from .salesforce_campaigns import prepare_membership

QUERY_VERSION = "campaign-state-grade-v1"
_ALLOWED_GRADES = frozenset({"gold", "silver", "watch"})
_NCES_ID_PATTERN = re.compile(r"(?:\d{7}|\d{12})")


class CampaignBatchGateway(Protocol):
    """Read/create surface used to prepare child Campaign actions."""

    def verify_write_scope(self) -> SalesforceOrganizationIdentity:
        """Return the exact configured writer organization."""
        ...

    def get_record(self, sobject: str, record_id: str) -> SalesforceRecordRef:
        """Read one exact Salesforce record."""
        ...

    def find_people(self, entity_name: str, state: str) -> list[SalesforceRecordRef]:
        """Find exact Lead/Contact candidates."""
        ...

    def find_accounts(self, entity_name: str, state: str) -> list[SalesforceRecordRef]:
        """Find exact Account candidates."""
        ...

    def resolve_organizations(
        self, organizations: list[tuple[str, str]]
    ) -> dict[str, tuple[list[SalesforceRecordRef], list[SalesforceRecordRef]]]:
        """Resolve exact organization records in bounded bulk queries."""
        ...


def _stable_json(value: object) -> str:
    """Serialize immutable batch evidence deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: object) -> str:
    """Hash one immutable batch selection or item."""
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _manifest_item(item: dict[str, object]) -> dict[str, object]:
    """Return the exact serializable fields protected by manifest hashes."""
    ref = item.get("salesforce_ref")
    return {
        "canonical_entity_key": item["canonical_entity_key"],
        "representative_lead_id": item["representative_lead_id"],
        "source_lead_ids": item["source_lead_ids"],
        "grades": item["grades"],
        "entity_name": item["entity_name"],
        "state": item["state"],
        "resolution_state": item["resolution_state"],
        "salesforce_sobject": ref.sobject if ref else "",
        "salesforce_id": ref.record_id if ref else "",
        "salesforce_account_id": ref.account_id if ref else "",
        "note": item["note"],
    }


def _selection_rows(
    conn: sqlite3.Connection, state: str, grades: tuple[str, ...]
) -> tuple[int, list[sqlite3.Row]]:
    """Count and select the complete state/tier set in one stable DB snapshot."""
    placeholders = ",".join("?" for _ in grades)
    params = [state, *grades]
    conn.execute("SAVEPOINT campaign_batch_selection")
    try:
        expected = int(
            conn.execute(
                f"""SELECT COUNT(*) FROM leads
                    WHERE {db.SEARCHABLE_LEAD_PREDICATE}
                      AND UPPER(state)=?
                      AND LOWER(lead_grade) IN ({placeholders})""",
                params,
            ).fetchone()[0]
        )
        rows = list(
            conn.execute(
                f"""SELECT id,entity_name,state,lead_grade,canonical_entity_key,nces_id
                FROM leads
                WHERE {db.SEARCHABLE_LEAD_PREDICATE}
                  AND UPPER(state)=?
                  AND LOWER(lead_grade) IN ({placeholders})
                ORDER BY id""",
                params,
            )
        )
        conn.execute("RELEASE SAVEPOINT campaign_batch_selection")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT campaign_batch_selection")
        conn.execute("RELEASE SAVEPOINT campaign_batch_selection")
        raise
    if len(rows) != expected:
        raise ValueError(
            "Campaign batch selection was incomplete; no preview was created"
        )
    return expected, rows


def _group_rows(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Aggregate every source row and tier under one authoritative organization."""
    by_name_state: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = db.canonical_entity_key(str(row["entity_name"]), str(row["state"] or ""))
        by_name_state[key].append(row)
    grouped: dict[str, tuple[list[sqlite3.Row], bool]] = {}
    for fallback_key, members in by_name_state.items():
        nces_ids = {
            str(member["nces_id"]).strip()
            for member in members
            if _NCES_ID_PATTERN.fullmatch(str(member["nces_id"] or "").strip())
        }
        if nces_ids:
            for nces_id in sorted(nces_ids):
                selected = [
                    member
                    for member in members
                    if str(member["nces_id"] or "").strip() == nces_id
                ]
                state = str(selected[0]["state"] or "").strip().upper()
                grouped[f"nces:{nces_id}|{state}"] = (selected, False)
            unmatched = [
                member
                for member in members
                if not _NCES_ID_PATTERN.fullmatch(str(member["nces_id"] or "").strip())
            ]
            if unmatched:
                # A name match is not evidence that an untagged row belongs to a
                # neighboring NCES entity. Preserve it separately and fail closed.
                grouped[f"unbound:{fallback_key}"] = (unmatched, True)
        else:
            grouped[fallback_key] = (members, len(members) > 1)
    organizations: list[dict[str, object]] = []
    for key in sorted(grouped):
        members, collision = grouped[key]
        organizations.append(
            {
                "canonical_entity_key": key,
                "representative_lead_id": int(members[0]["id"]),
                "source_lead_ids": [int(item["id"]) for item in members],
                "grades": sorted({str(item["lead_grade"]).lower() for item in members}),
                "entity_name": str(members[0]["entity_name"]),
                "state": str(members[0]["state"] or "").strip().upper(),
                "identity_collision": collision,
            }
        )
    return organizations


def _exact_people(
    gateway: CampaignBatchGateway, organization: dict[str, object]
) -> tuple[list[SalesforceRecordRef], list[SalesforceRecordRef]]:
    """Return strict person and Account matches for one frozen organization."""
    name = str(organization["entity_name"])
    state = str(organization["state"])
    people = [
        candidate
        for candidate in gateway.find_people(name, state)
        if record_matches_organization(candidate, name, state)
    ]
    accounts = [
        candidate
        for candidate in gateway.find_accounts(name, state)
        if record_matches_organization(candidate, name, state)
    ]
    return people, accounts


def _resolution(
    people: list[SalesforceRecordRef], accounts: list[SalesforceRecordRef]
) -> tuple[str, SalesforceRecordRef | None, str]:
    """Classify a safe Campaign-member identity without fuzzy selection."""
    if len(people) == 1:
        return "existing_record", people[0], ""
    if len(people) > 1:
        return "ambiguous", None, "Multiple exact Leads/Contacts require selection."
    if len(accounts) == 1:
        return (
            "account_only",
            accounts[0],
            "An exact Account exists but no exact Lead/Contact can be a Campaign Member.",
        )
    if len(accounts) > 1:
        return (
            "ambiguous",
            None,
            "Multiple exact Salesforce Accounts require selection.",
        )
    return "missing", None, "No exact Salesforce Lead, Contact, or Account was found."


def _insert_manifest(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    identity: SalesforceOrganizationIdentity,
    targets: list[dict[str, object]],
    state: str,
    completion_mode: str,
) -> None:
    """Persist the immutable parent, target, and organization selections."""
    now = iso_timestamp(now_utc())
    all_items = [item for target in targets for item in target["items"]]
    with conn:
        conn.execute(
            """INSERT INTO crm_campaign_batches
                (id,workspace,channel,thread_ts,requested_by,query_version,state,
                  completion_mode,
                  expected_source_row_count,stored_source_row_count,source_row_count,
                  unique_org_count,selection_hash,writer_org_id,writer_is_sandbox,
                  writer_host,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                batch_id,
                workspace,
                channel,
                thread_ts,
                requester,
                QUERY_VERSION,
                state,
                completion_mode,
                sum(int(target["expected_source_row_count"]) for target in targets),
                sum(int(target["source_row_count"]) for target in targets),
                sum(int(target["source_row_count"]) for target in targets),
                len(all_items),
                _hash(
                    [
                        {
                            "campaign_id": target["campaign"].record_id,
                            "state": target["state"],
                            "grades": target["grades"],
                            "selection_hash": _hash(
                                [_manifest_item(item) for item in target["items"]]
                            ),
                        }
                        for target in targets
                    ]
                ),
                identity.organization_id,
                int(identity.is_sandbox),
                (urlparse(identity.instance_url).hostname or "").lower(),
                now,
                now,
            ),
        )
        for target in targets:
            campaign = target["campaign"]
            approved_items = [
                item
                for item in target["items"]
                if target["completion_mode"] == "full"
                or item["resolution_state"] == "existing_record"
            ]
            approved_keys = sorted(
                str(item["canonical_entity_key"]) for item in approved_items
            )
            conn.execute(
                """INSERT INTO crm_campaign_batch_targets
                     (id,batch_id,campaign_id,campaign_name,campaign_link,state_code,
                      grades_json,expected_source_row_count,stored_source_row_count,
                      source_row_count,unique_org_count,selection_hash,
                      approved_org_count,approved_selection_hash,completion_mode,state,
                      created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    target["id"],
                    batch_id,
                    campaign.record_id,
                    campaign.name,
                    campaign.link,
                    target["state"],
                    _stable_json(target["grades"]),
                    target["expected_source_row_count"],
                    target["source_row_count"],
                    target["source_row_count"],
                    len(target["items"]),
                    _hash([_manifest_item(item) for item in target["items"]]),
                    len(approved_items),
                    _hash(approved_keys),
                    target["completion_mode"],
                    target["target_state"],
                    now,
                    now,
                ),
            )
            for item in target["items"]:
                ref = item.get("salesforce_ref")
                conn.execute(
                    """INSERT INTO crm_campaign_batch_items
                         (id,target_id,canonical_entity_key,representative_lead_id,
                          source_lead_ids_json,grades_json,entity_name,state_code,
                          item_hash,resolution_state,salesforce_sobject,salesforce_id,
                          salesforce_account_id,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        target["id"],
                        item["canonical_entity_key"],
                        item["representative_lead_id"],
                        _stable_json(item["source_lead_ids"]),
                        _stable_json(item["grades"]),
                        item["entity_name"],
                        item["state"],
                        _hash(_manifest_item(item)),
                        item["resolution_state"],
                        ref.sobject if ref else None,
                        ref.record_id if ref else None,
                        ref.account_id if ref else None,
                        item["note"] or None,
                    ),
                )


def _target_summary(target: dict[str, object]) -> str:
    """Render exact source, organization, and resolution counts for one Campaign."""
    counts: dict[str, int] = defaultdict(int)
    for item in target["items"]:
        counts[str(item["resolution_state"])] += 1
    slice_count = int(target.get("slice_count", 1) or 1)
    position = ""
    if slice_count > 1:
        position = (
            f" [batch {int(target.get('slice_index', 0)) + 1} of {slice_count}; "
            f"{target.get('total_organizations')} organizations in total, "
            f"{MAX_ACTION_ORGANIZATIONS} per batch]"
        )
    summary = (
        f"• {target['campaign'].name}{position}: {target['source_row_count']} Grant "
        f"rows, {len(target['items'])} unique organizations — "
        f"{counts['existing_record']} exact members, {counts['missing']} missing, "
        f"{counts['account_only']} Account-only, {counts['ambiguous']} ambiguous"
    )
    unresolved = [
        item
        for item in target["items"]
        if item["resolution_state"] != "existing_record"
    ]
    if not unresolved:
        return summary
    details = "\n".join(
        f"  - {item['entity_name']} ({item['state']}): "
        f"{item['resolution_state']} — {item['note']}"
        for item in unresolved
    )
    return f"{summary}\n  Unresolved organizations:\n{details}"


def _materialize_target_action(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    *,
    batch_id: str,
    target: dict[str, object],
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    allow_org_leads: bool,
    allow_resolved_only: bool,
) -> PreparedAction | None:
    """Create and link one isolated child action from a frozen target manifest."""
    included = list(target["items"])
    if target["completion_mode"] == "partial_by_user":
        included = [
            item for item in included if item["resolution_state"] == "existing_record"
        ]
    lead_ids = [int(item["representative_lead_id"]) for item in included]
    if not lead_ids:
        return None
    frozen_records = {
        int(item["representative_lead_id"]): item["salesforce_ref"]
        for item in included
        if item["resolution_state"] == "existing_record"
        and item["salesforce_ref"] is not None
    }
    canonical_keys = {
        int(item["representative_lead_id"]): str(item["canonical_entity_key"])
        for item in included
    }
    approved_keys = sorted(canonical_keys.values())
    action: PreparedAction | None = None
    try:
        action = prepare_membership(
            conn,
            gateway,
            workspace,
            channel,
            thread_ts,
            requester,
            target["campaign"],
            lead_ids,
            resolved_records=frozen_records,
            canonical_keys=canonical_keys,
            allow_org_leads=allow_org_leads,
        )
        action_rows = list(
            conn.execute(
                """SELECT id,canonical_entity_key FROM crm_action_items
                   WHERE action_id=? ORDER BY canonical_entity_key""",
                (action.action_id,),
            )
        )
        action_keys = [str(row["canonical_entity_key"]) for row in action_rows]
        if action_keys != approved_keys:
            raise ValueError(
                "Prepared Salesforce action did not preserve every approved organization"
            )
        with conn:
            conn.execute(
                """UPDATE crm_actions SET batch_id=?,batch_target_id=? WHERE id=?""",
                (batch_id, target["id"], action.action_id),
            )
            conn.execute(
                """UPDATE crm_campaign_batch_targets
                   SET action_id=?,state='approval_ready',updated_at=? WHERE id=?""",
                (action.action_id, iso_timestamp(now_utc()), target["id"]),
            )
            conn.execute(
                """UPDATE crm_campaign_batch_items
                   SET crm_action_item_id=(
                     SELECT id FROM crm_action_items
                     WHERE action_id=? AND canonical_entity_key=
                           crm_campaign_batch_items.canonical_entity_key
                   )
                   WHERE target_id=?""",
                (action.action_id, target["id"]),
            )
            linked_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM crm_campaign_batch_items
                       WHERE target_id=? AND crm_action_item_id IS NOT NULL""",
                    (target["id"],),
                ).fetchone()[0]
            )
            if linked_count != len(approved_keys):
                raise ValueError(
                    "Campaign manifest could not map every approved organization "
                    "to one action item"
                )
    except Exception:
        if action is not None:
            with conn:
                conn.execute(
                    """UPDATE crm_actions SET state='cancelled',last_error=?,updated_at=?
                       WHERE id=? AND state='ready'""",
                    (
                        "Batch target linking failed before Slack delivery",
                        iso_timestamp(now_utc()),
                        action.action_id,
                    ),
                )
        raise
    if action is None:
        raise RuntimeError("Campaign action preparation produced no action")
    return action


def _prepare_campaign_batch(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    requests: tuple[CampaignTargetRequest, ...],
    *,
    allow_org_leads: bool = False,
    allow_resolved_only: bool = False,
) -> PreparedCampaignBatch:
    """Freeze and resolve a multi-Campaign request, then prepare safe child actions."""
    validate_action_context(workspace, channel, thread_ts, requester)
    if not requests:
        raise ValueError("At least one Campaign target is required")
    identity = gateway.verify_write_scope()
    batch_id = str(uuid.uuid4())
    targets: list[dict[str, object]] = []
    for request in requests:
        state = request.state.strip().upper()
        grades = tuple(sorted({grade.strip().lower() for grade in request.grades}))
        if len(state) != 2 or not grades or not set(grades) <= _ALLOWED_GRADES:
            raise ValueError(
                "Each target needs a two-letter state and valid Grant tiers"
            )
        _sobject, campaign_id = parse_record_link(request.campaign_link, {"Campaign"})
        campaign = gateway.get_record("Campaign", campaign_id)
        expected_count, rows = _selection_rows(conn, state, grades)
        organizations = _group_rows(rows)
        if not organizations:
            raise ValueError(f"No Grant leads matched {state} and {', '.join(grades)}")
        # Salesforce takes at most 200 records per collection call, and a whole
        # state and tier is routinely larger. This used to raise, telling the rep to
        # "refine the request" — advice this tool CANNOT take, because its only
        # filters are state and grade and both were already at their finest. So the
        # selection is cut into ordered slices instead, and the rep is told which
        # one they are looking at and how many remain.
        #
        # Slices come from ONE grouped selection, computed once above: cutting per
        # slice would let a lead added by the 07:00 poll shift membership between
        # slice 1 and slice 2, so an organization could appear in both or neither.
        # _group_rows returns a stable order, so slice boundaries are deterministic.
        total_organizations = len(organizations)
        slice_count = max(
            1,
            (total_organizations + MAX_ACTION_ORGANIZATIONS - 1)
            // MAX_ACTION_ORGANIZATIONS,
        )
        slice_index = max(0, int(request.slice_index))
        expected_total = int(request.expected_total_organizations)
        if slice_index > 0 and expected_total and expected_total != total_organizations:
            raise ValueError(
                f"{state} had {expected_total} organizations when batch 1 was "
                f"prepared and has {total_organizations} now, so the batches no "
                "longer line up and one could be skipped entirely. Start the "
                "batches again from the first one."
            )
        if slice_index >= slice_count:
            raise ValueError(
                f"{state} has {total_organizations} organizations, which is "
                f"{slice_count} batch(es) of {MAX_ACTION_ORGANIZATIONS}; "
                f"batch {slice_index + 1} does not exist"
            )
        window_start = slice_index * MAX_ACTION_ORGANIZATIONS
        organizations = organizations[
            window_start : window_start + MAX_ACTION_ORGANIZATIONS
        ]
        bulk_resolver = getattr(gateway, "resolve_organizations", None)
        bulk_results = (
            bulk_resolver(
                [
                    (str(item["entity_name"]), str(item["state"]))
                    for item in organizations
                    if not bool(item["identity_collision"])
                ]
            )
            if callable(bulk_resolver)
            else {}
        )
        resolved_items: list[dict[str, object]] = []
        for organization in organizations:
            if bool(organization["identity_collision"]):
                resolution_state, ref, note = (
                    "ambiguous",
                    None,
                    "Multiple Grant rows share only a name/state identity.",
                )
            else:
                lookup_key = db.canonical_entity_key(
                    str(organization["entity_name"]), str(organization["state"])
                )
                if lookup_key in bulk_results:
                    people, accounts = bulk_results[lookup_key]
                else:
                    people, accounts = _exact_people(gateway, organization)
                people = [
                    item
                    for item in people
                    if record_matches_organization(
                        item,
                        str(organization["entity_name"]),
                        str(organization["state"]),
                    )
                ]
                accounts = [
                    item
                    for item in accounts
                    if record_matches_organization(
                        item,
                        str(organization["entity_name"]),
                        str(organization["state"]),
                    )
                ]
                resolution_state, ref, note = _resolution(people, accounts)
            resolved_items.append(
                {
                    **organization,
                    "resolution_state": resolution_state,
                    "salesforce_ref": ref,
                    "note": note,
                }
            )
        # Distinct authoritative Grant organizations may never collapse onto
        # one Salesforce Lead/Contact. That would make a complete-looking action
        # silently omit an approved organization.
        by_salesforce_id: dict[str, list[dict[str, object]]] = defaultdict(list)
        for item in resolved_items:
            ref = item.get("salesforce_ref")
            if item["resolution_state"] == "existing_record" and ref is not None:
                by_salesforce_id[str(ref.record_id)].append(item)
        for record_id, duplicates in by_salesforce_id.items():
            if len(duplicates) < 2:
                continue
            for item in duplicates:
                item["resolution_state"] = "ambiguous"
                item["salesforce_ref"] = None
                item["note"] = (
                    f"Salesforce record {record_id} matched multiple authoritative "
                    "Grant organizations."
                )
        unresolved = [
            item
            for item in resolved_items
            if item["resolution_state"] != "existing_record"
        ]
        target_state = "blocked_resolution" if unresolved else "ready"
        targets.append(
            {
                "id": str(uuid.uuid4()),
                "campaign": campaign,
                "state": state,
                "grades": list(grades),
                "expected_source_row_count": expected_count,
                "source_row_count": len(rows),
                "items": resolved_items,
                "target_state": target_state,
                "completion_mode": "full",
                "slice_index": slice_index,
                "slice_count": slice_count,
                "total_organizations": total_organizations,
            }
        )
    blocked = any(
        item["resolution_state"] != "existing_record"
        for target in targets
        for item in target["items"]
    )
    hard_blocked = any(
        item["resolution_state"] in {"ambiguous", "account_only"}
        for target in targets
        for item in target["items"]
    )
    batch_state = "blocked_resolution" if blocked else "ready"
    if blocked and allow_resolved_only:
        batch_state = "partial_by_user"
        for target in targets:
            if any(
                item["resolution_state"] != "existing_record"
                for item in target["items"]
            ):
                target["target_state"] = "partial_by_user"
                target["completion_mode"] = "partial_by_user"
    _insert_manifest(
        conn,
        batch_id=batch_id,
        workspace=workspace,
        channel=channel,
        thread_ts=thread_ts,
        requester=requester,
        identity=identity,
        targets=targets,
        state=batch_state,
        completion_mode=(
            "partial_by_user" if blocked and allow_resolved_only else "full"
        ),
    )
    lines = [_target_summary(target) for target in targets]
    if blocked and not allow_resolved_only and (not allow_org_leads or hard_blocked):
        summary = (
            f"Salesforce batch {batch_id} is blocked before approval.\n"
            + "\n".join(lines)
            + "\nNo confirmation buttons were created. Resolve exact records or "
            "explicitly approve organization-only Leads for missing organizations; "
            "existing Accounts and ambiguous identities are never silently duplicated."
        )
        return PreparedCampaignBatch(batch_id, summary)

    actions: list[PreparedAction] = []
    try:
        for target in targets:
            action = _materialize_target_action(
                conn,
                gateway,
                batch_id=batch_id,
                target=target,
                workspace=workspace,
                channel=channel,
                thread_ts=thread_ts,
                requester=requester,
                allow_org_leads=allow_org_leads,
                allow_resolved_only=allow_resolved_only,
            )
            if action is not None:
                actions.append(action)
    except Exception as exc:
        # A later target can fail ownership or validation after an earlier target
        # prepared successfully. Cancel every undelivered child so no orphan nonce
        # remains executable when the tool returns only an error.
        with conn:
            for action in actions:
                conn.execute(
                    """UPDATE crm_actions SET state='cancelled',last_error=?,updated_at=?
                       WHERE id=? AND state='ready'""",
                    (
                        "Batch preparation failed before Slack delivery",
                        iso_timestamp(now_utc()),
                        action.action_id,
                    ),
                )
            conn.execute(
                """UPDATE crm_campaign_batches SET state='failed',updated_at=? WHERE id=?""",
                (iso_timestamp(now_utc()), batch_id),
            )
            conn.execute(
                """UPDATE crm_campaign_batch_targets
                   SET state='failed',last_error=?,updated_at=? WHERE batch_id=?""",
                (
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    iso_timestamp(now_utc()),
                    batch_id,
                ),
            )
        raise
    if not actions:
        return PreparedCampaignBatch(
            batch_id,
            "No fully resolved organizations remain for approval; no buttons were created.",
            state="partial_by_user" if allow_resolved_only else "blocked_resolution",
        )
    summary = (
        f"Salesforce batch {batch_id} froze these exact selections:\n"
        + "\n".join(lines)
        + (
            "\nOnly the exact resolved subset is included; the batch is permanently "
            "marked partial by user choice."
            if allow_resolved_only and blocked
            else "\nOne isolated confirmation follows for each Campaign."
        )
    )
    final_batch_state = (
        "partial_approval_ready"
        if allow_resolved_only and blocked
        else "approval_ready"
    )
    with conn:
        conn.execute(
            "UPDATE crm_campaign_batches SET state=?,updated_at=? WHERE id=?",
            (final_batch_state, iso_timestamp(now_utc()), batch_id),
        )
    return PreparedCampaignBatch(
        batch_id,
        summary,
        tuple(actions),
        "partial_by_user" if allow_resolved_only and blocked else "approval_ready",
    )


def _record_attempt(
    conn: sqlite3.Connection,
    *,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    requests: tuple[CampaignTargetRequest, ...],
) -> str:
    """Persist that this attempt STARTED, before anything can refuse it.

    Deliberately the very first thing that happens. Every validation failure below
    raises before the manifest is written, so without this row a refused request
    leaves no trace at all — which is exactly what made an SDR's dead-end invisible
    afterwards, and what stops a follow-up worker from ever noticing it.
    """
    attempt_id = str(uuid.uuid4())
    payload = [
        {
            "campaign_link": item.campaign_link,
            "state": item.state,
            "grades": list(item.grades),
            "slice_index": item.slice_index,
        }
        for item in requests
    ]
    with conn:
        conn.execute(
            """INSERT INTO crm_campaign_attempts
                 (id,workspace,channel,thread_ts,requested_by,request_json,state,
                  started_at)
               VALUES (?,?,?,?,?,?, 'started', ?)""",
            (
                attempt_id,
                workspace,
                channel,
                thread_ts,
                requester,
                _stable_json(payload),
                iso_timestamp(now_utc()),
            ),
        )
    return attempt_id


def _close_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    state: str,
    batch_id: str = "",
    failure: BaseException | None = None,
) -> None:
    """Record how the attempt ended, including WHY when it was refused."""
    with conn:
        conn.execute(
            """UPDATE crm_campaign_attempts
                  SET state=?,batch_id=?,failure_kind=?,failure_detail=?,finished_at=?
                WHERE id=?""",
            (
                state,
                batch_id or None,
                type(failure).__name__ if failure is not None else None,
                str(failure)[:400] if failure is not None else None,
                iso_timestamp(now_utc()),
                attempt_id,
            ),
        )


def prepare_campaign_batch(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    requests: tuple[CampaignTargetRequest, ...],
    *,
    allow_org_leads: bool = False,
    allow_resolved_only: bool = False,
) -> PreparedCampaignBatch:
    """Freeze and resolve a multi-Campaign request, recording the attempt either way.

    A thin durable wrapper: the attempt row is written first and closed last, so a
    refusal is as visible in the database as a success. The refusal itself still
    propagates unchanged — callers and their error messages are unaffected.
    """
    attempt_id = _record_attempt(
        conn,
        workspace=workspace,
        channel=channel,
        thread_ts=thread_ts,
        requester=requester,
        requests=requests,
    )
    try:
        prepared = _prepare_campaign_batch(
            conn,
            gateway,
            workspace,
            channel,
            thread_ts,
            requester,
            requests,
            allow_org_leads=allow_org_leads,
            allow_resolved_only=allow_resolved_only,
        )
    except BaseException as exc:
        _close_attempt(conn, attempt_id, state="failed", failure=exc)
        raise
    _close_attempt(conn, attempt_id, state="prepared", batch_id=prepared.batch_id)
    return prepared
