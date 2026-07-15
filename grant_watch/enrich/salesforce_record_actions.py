"""Audited standalone Salesforce Lead and Opportunity create actions.

Campaign membership remains in ``salesforce_campaigns``. This module owns the two
single-record workflows so each responsibility stays below the project file-size cap.
It reuses the shared immutable approval ledger and never exposes update or delete.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

import requests

from . import salesforce
from . import salesforce_campaigns as workflow
from .salesforce_campaign_gateway import (
    SalesforceCompositeRolledBack,
    SalesforceCampaignGateway,
    parse_record_link,
    validate_record_id,
)
from .organization_profile import OrganizationProfile, fetch_profile


@dataclass(frozen=True)
class PersonLeadDraft:
    """One persisted verified contact proposed as a standalone Salesforce Lead."""

    contact_id: int
    grant_lead_id: int
    person_name: str
    company: str
    email: str
    state: str
    title: str
    phone: str
    source_url: str
    organization: OrganizationProfile
    enrollment: int | None
    industry: str

    def payload(self, action_id: str, requester: str) -> dict[str, object]:
        """Return exact Salesforce fields; split only an unambiguous two-token name."""
        name_parts = self.person_name.split()
        simple_name = (len(name_parts) == 2 and all(
            re.fullmatch(r"[A-Za-z][A-Za-z'’-]*", part) for part in name_parts))
        research = [
            f"Grant research source: {self.organization.source_url or self.source_url}",
            f"Verified contact source: {self.source_url}",
        ]
        if self.organization.main_phone:
            research.append(
                f"Official organization main phone: {self.organization.main_phone}")
        if self.enrollment is not None:
            research.append(f"NCES district enrollment: {self.enrollment}")
        result: dict[str, object] = {
            "Company": self.company,
            "LastName": name_parts[1] if simple_name else self.person_name,
            "Email": self.email, "Status": "New", "LeadSource": "Other",
            "Description": (
                f"Created by Grant from verified public contact {self.contact_id} for "
                f"Grant lead {self.grant_lead_id}. Evidence: {self.source_url}. "
                f"Action {action_id}. Requested by Slack user {requester}.\n"
                + "\n".join(research)),
        }
        if simple_name:
            result["FirstName"] = name_parts[0]
        if self.state:
            result["State"] = self.state
        if self.title:
            result["Title"] = self.title
        if self.phone:
            result["Phone"] = self.phone
        elif self.organization.main_phone:
            result["Phone"] = self.organization.main_phone
        for key, value in (
            ("Website", self.organization.website),
            ("Street", self.organization.street),
            ("City", self.organization.city),
            ("State", self.organization.state or self.state),
            ("PostalCode", self.organization.postal_code),
            ("Country", self.organization.country),
            ("LinkedIn__c", self.organization.linkedin_url),
            ("Industry", self.industry),
        ):
            if value:
                result[key] = value
        if self.enrollment is not None:
            result["Number_of_Students__c"] = self.enrollment
        return result


@dataclass(frozen=True)
class OpportunityDraft:
    """Allowlisted fields for one Opportunity under an exact existing Account."""

    account_id: str
    account_name: str
    name: str
    stage_name: str
    close_date: str
    owner_id: str
    owner_name: str
    amount: float | None = None

    def payload(self, action_id: str, requester: str) -> dict[str, object]:
        """Return only the approved Salesforce Opportunity create fields."""
        result: dict[str, object] = {
            "AccountId": self.account_id, "Name": self.name,
            "StageName": self.stage_name, "CloseDate": self.close_date,
            "OwnerId": self.owner_id,
            "Description": f"Created by Grant. Action {action_id}. Requested by {requester}.",
        }
        if self.amount is not None:
            result["Amount"] = self.amount
        return result


def duplicate_person(email: str, company: str, state: str) -> list[salesforce.SFMatch]:
    """Fail closed on exact email or any plausible organization CRM match."""
    exact = salesforce.exact_email_matches(email)
    if exact:
        return exact
    result = salesforce.lookup(company, state=state)
    if result.status in {salesforce.SFResultStatus.UNAVAILABLE,
                         salesforce.SFResultStatus.PARTIAL}:
        raise ConnectionError(result.error or "Salesforce duplicate check incomplete")
    return result.matches


def _audit_task_description(action: str, verb: str, fields: list[str],
                            original_time: str = "") -> str:
    """Describe one CRM API action honestly without implying customer outreach."""
    field_text = ", ".join(sorted(fields)) or "audit records only"
    timing = f" Original Lead update completed at {original_time}." if original_time else ""
    return (
        f"Grant {verb} these Salesforce Lead fields: {field_text}.{timing} "
        f"No customer outreach was performed. Action {action}."
    )


def _activity_date() -> str:
    """Return today's local calendar date for the Salesforce administrative Task."""
    return datetime.now().astimezone().date().isoformat()


def prepare_person_lead_creation(
        conn: sqlite3.Connection, workspace: str, channel: str, thread_ts: str,
        requester: str, contact_id: int) -> workflow.PreparedAction:
    """Freeze a verified contact and duplicate-safe one-Lead confirmation preview."""
    workflow._validate_context(workspace, channel, thread_ts, requester)
    row = conn.execute(
        """SELECT c.*,l.entity_name,l.state AS lead_state,l.canonical_entity_key,
                  l.entity_type,l.enrollment
             FROM contacts c JOIN leads l ON l.id=c.lead_id WHERE c.id=?""",
        (contact_id,)).fetchone()
    if row is None or str(row["contact_status"] or "") != "verified":
        raise ValueError("contact is not verified")
    evidence = json.loads(str(row["field_evidence_json"] or "{}"))
    name, email, source = (str(row[key] or "").strip()
                           for key in ("name", "email", "source_url"))
    if not (evidence.get("name") and evidence.get("email") and name and source):
        raise ValueError("contact name and email require current source evidence")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("contact email is invalid")
    company = str(row["entity_name"] or "").strip()
    state = str(row["lead_state"] or "").strip().upper()
    duplicates = workflow._duplicate_person(email, company, state)
    if duplicates:
        links = ", ".join(item.link for item in duplicates[:3])
        raise ValueError(f"Salesforce already has a possible matching record: {links}")
    action_id = str(uuid.uuid4())
    try:
        organization = fetch_profile(
            company, str(row["official_domain"] or ""), source)
    except (KeyError, ValueError, RuntimeError, requests.RequestException):
        organization = OrganizationProfile(
            website=(f"https://{str(row['official_domain']).strip()}/"
                     if row["official_domain"] else ""), source_url=source)
    entity_text = f"{row['entity_type'] or ''} {company}".lower()
    industry = "K-12 Schools" if any(
        word in entity_text for word in ("school", "district", "k-12")) else ""
    enrollment = int(row["enrollment"]) if row["enrollment"] is not None else None
    draft = PersonLeadDraft(
        contact_id, int(row["lead_id"]), name, company, email, state,
        str(row["title"] or "").strip() if evidence.get("title") else "",
        str(row["phone"] or "").strip() if evidence.get("phone") else "", source,
        organization, enrollment, industry)
    payload = draft.payload(action_id, requester)
    plan = workflow.MemberPlan(
        draft.grant_lead_id, str(row["canonical_entity_key"] or company.lower()),
        company, state, "create_verified_person_lead", proposed_lead=payload,
        note=json.dumps({"contact_id": contact_id, "source_url": source}))
    stored_id, nonce, expires = workflow._store_action(
        conn, "create_person_lead", workspace, channel, thread_ts, requester,
        {"lead": payload, "contact_id": contact_id, "source_url": source},
        plans=[plan], action_id=action_id)
    optional = "".join((f"\n• Title: {draft.title}" if draft.title else "",
                        f"\n• Phone: {draft.phone}" if draft.phone else ""))
    preview = (f"Create this Salesforce Lead?\n• Name: {name}\n• Organization: {company}"
               f"\n• Email: {email}{optional}\n• Source: {source}"
               "\nNo Campaign membership will be created.")
    enriched = [
        ("Website", organization.website), ("Phone", organization.main_phone),
        ("Address", " ".join(filter(None, (organization.street, organization.city,
                                            organization.state,
                                            organization.postal_code)))),
        ("Industry", industry),
        ("Students", f"{enrollment:,}" if enrollment is not None else ""),
        ("LinkedIn", organization.linkedin_url),
    ]
    preview += "".join(f"\n• {label}: {value}" for label, value in enriched if value)
    preview += "\n• Add a visible Salesforce Note with Grant’s verified research sources"
    preview += "\n• Add a system Activity describing exactly what Grant created"
    preview += "\n• The Activity will say that no customer outreach occurred"
    return workflow.PreparedAction(stored_id, nonce, preview, expires)


def prepare_opportunity_creation(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        account_link: str, name: str, stage_name: str, close_date: str,
        owner_id: str, owner_name: str,
        amount: float | None = None) -> workflow.PreparedAction:
    """Freeze one duplicate-checked Opportunity preview under an exact Account."""
    workflow._validate_context(workspace, channel, thread_ts, requester)
    _sobject, account_id = parse_record_link(account_link, {"Account"})
    account = gateway.get_record("Account", account_id)
    clean_name = " ".join(name.split())
    if not clean_name or len(clean_name) > 120:
        raise ValueError("Opportunity name must be between 1 and 120 characters")
    if stage_name not in gateway.opportunity_stages():
        raise ValueError("Opportunity stage is not active in Salesforce")
    try:
        datetime.strptime(close_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Opportunity close date must be YYYY-MM-DD") from exc
    validate_record_id(owner_id, "User")
    if amount is not None and (not math.isfinite(amount) or amount < 0):
        raise ValueError("Opportunity amount must be a finite nonnegative number")
    duplicate = next((item for item in gateway.open_opportunities(account_id)
                      if item.name.casefold() == clean_name.casefold()), None)
    if duplicate:
        raise ValueError(f"That open Opportunity already exists: {duplicate.link}")
    action_id = str(uuid.uuid4())
    draft = OpportunityDraft(account_id, account.name, clean_name, stage_name,
                             close_date, owner_id, owner_name, amount)
    payload = draft.payload(action_id, requester)
    plan = workflow.MemberPlan(
        None, f"account:{account_id}", account.name, account.state,
        "create_opportunity", proposed_lead=payload)
    stored_id, nonce, expires = workflow._store_action(
        conn, "create_opportunity", workspace, channel, thread_ts, requester,
        {"opportunity": payload, "account_link": account.link}, plans=[plan],
        action_id=action_id)
    amount_line = f"\n• Amount: ${amount:,.2f}" if amount is not None else ""
    preview = (f"Create this Salesforce Opportunity?\n• Account: {account.name}"
               f"\n• Name: {clean_name}\n• Stage: {stage_name}"
               f"\n• Close date: {close_date}{amount_line}\n• Owner: {owner_name}")
    return workflow.PreparedAction(stored_id, nonce, preview, expires)


def prepare_lead_enrichment(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        contact_id: int, lead_link: str) -> workflow.PreparedAction:
    """Prepare a fill-blank-only update for one exact matching Salesforce Lead."""
    workflow._validate_context(workspace, channel, thread_ts, requester)
    _sobject, salesforce_id = parse_record_link(lead_link, {"Lead"})
    row = conn.execute(
        """SELECT c.*,l.entity_name,l.state AS lead_state,l.entity_type,l.enrollment,
                  l.canonical_entity_key
             FROM contacts c JOIN leads l ON l.id=c.lead_id WHERE c.id=?""",
        (contact_id,)).fetchone()
    if row is None or str(row["contact_status"] or "") != "verified":
        raise ValueError("contact is not verified")
    evidence = json.loads(str(row["field_evidence_json"] or "{}"))
    if not evidence.get("email") or not row["source_url"] or not row["official_domain"]:
        raise ValueError("contact email and official domain require current evidence")
    snapshot = gateway.lead_enrichment_snapshot(salesforce_id)
    company, email = str(row["entity_name"] or ""), str(row["email"] or "")
    if snapshot.company.casefold() != company.casefold() or snapshot.email.casefold() != email.casefold():
        raise ValueError("Salesforce Lead does not match the verified contact and organization")
    try:
        organization = fetch_profile(
            company, str(row["official_domain"]), str(row["source_url"]))
    except (KeyError, ValueError, RuntimeError, requests.RequestException):
        organization = OrganizationProfile(
            website=f"https://{str(row['official_domain']).strip()}/",
            source_url=str(row["source_url"]))
    entity_text = f"{row['entity_type'] or ''} {company}".lower()
    industry = "K-12 Schools" if any(
        word in entity_text for word in ("school", "district", "k-12")) else ""
    desired: dict[str, object] = {
        "Website": organization.website, "Phone": organization.main_phone,
        "Street": organization.street, "City": organization.city,
        "State": organization.state or str(row["lead_state"] or ""),
        "PostalCode": organization.postal_code, "Country": organization.country,
        "Industry": industry, "LinkedIn__c": organization.linkedin_url,
    }
    if row["enrollment"] is not None:
        desired["Number_of_Students__c"] = int(row["enrollment"])
    delta = {key: value for key, value in desired.items()
             if value not in (None, "") and snapshot.values.get(key) in (None, "")}
    action_id = str(uuid.uuid4())
    research = (
        f"Grant research — action {action_id}\n"
        f"Official organization source: {organization.source_url}\n"
        f"Verified contact source: {row['source_url']}")
    existing_description = str(snapshot.values.get("Description") or "").strip()
    delta["Description"] = f"{existing_description}\n\n{research}".strip()
    if set(delta) == {"Description"} and research in existing_description:
        raise ValueError("Salesforce Lead already contains every verified enrichment field")
    plan = workflow.MemberPlan(
        int(row["lead_id"]), str(row["canonical_entity_key"] or company.lower()),
        company, str(row["lead_state"] or ""), "enrich_existing_lead",
        proposed_lead=delta,
        note=json.dumps({"salesforce_id": salesforce_id,
                         "system_modstamp": snapshot.system_modstamp}))
    stored_id, nonce, expires = workflow._store_action(
        conn, "enrich_existing_lead", workspace, channel, thread_ts, requester,
        {"lead_id": salesforce_id, "delta": delta,
         "system_modstamp": snapshot.system_modstamp,
         "company": company, "email": email}, plans=[plan], action_id=action_id)
    labels = {"Number_of_Students__c": "Students", "LinkedIn__c": "LinkedIn"}
    lines = [f"• {labels.get(key, key)}: {value}" for key, value in delta.items()
             if key != "Description"]
    preview = "Fill these blank Salesforce Lead fields?\n" + "\n".join(lines)
    preview += "\n• Append the verified Grant research sources to Description"
    preview += "\n• Add the same verified research summary as a visible Salesforce Note"
    preview += "\n• Add a system Activity describing exactly which fields Grant updated"
    preview += "\n• The Activity will say that no customer outreach occurred"
    return workflow.PreparedAction(stored_id, nonce, preview, expires)


def confirm_person_lead(conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
                        row: sqlite3.Row) -> workflow.ActionExecution:
    """Recheck duplicates, create one Lead, and verify exact-email readback."""
    payload = dict(json.loads(str(row["payload_json"]))["lead"])
    email, company = str(payload["Email"]), str(payload["Company"])
    duplicates = workflow._duplicate_person(
        email, company, str(payload.get("State") or ""))
    if duplicates:
        item = duplicates[0]
        workflow._finish_action(
            conn, str(row["id"]), workflow.CampaignActionState.COMPLETE)
        with conn:
            conn.execute(
                """UPDATE crm_action_items SET state='already_present',salesforce_id=?
                   WHERE action_id=?""", (item.record_id, row["id"]))
        return workflow.ActionExecution(
            workflow.CampaignActionState.COMPLETE,
            f"{item.name or payload['LastName']} is already in Salesforce: {item.link}",
            already_present=1)
    workflow._mark_external_write_started(conn, str(row["id"]))
    action_id = str(row["id"])
    note_body = str(payload.get("Description") or "")
    task_body = _audit_task_description(
        action_id, "created and populated", list(payload))
    result = gateway.create_person_lead_with_audit_bundle(
        payload, action_id, note_body, task_body, _activity_date())
    if not result.success or not result.lead_id:
        raise SalesforceCompositeRolledBack(
            result.error or "Salesforce returned no Lead and audit IDs")
    validate_record_id(result.lead_id, "Lead")
    created = [item for item in salesforce.exact_email_matches(email)
               if item.record_id == result.lead_id]
    if len(created) != 1 or created[0].company != company:
        raise ValueError("created Lead could not be verified by exact readback")
    if not gateway.verify_lead_audit_bundle(
            result.lead_id, action_id, note_body, task_body, result):
        raise ValueError("created Salesforce Lead audit trail could not be verified")
    with conn:
        conn.execute(
            """UPDATE crm_action_items SET state='lead_created',salesforce_id=?
               WHERE action_id=?""", (result.lead_id, row["id"]))
    workflow._finish_action(
        conn, str(row["id"]), workflow.CampaignActionState.COMPLETE)
    return workflow.ActionExecution(
        workflow.CampaignActionState.COMPLETE,
        f"Created {created[0].name or payload['LastName']} in Salesforce: "
        f"{created[0].link}", added=1)


def confirm_opportunity(conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
                        row: sqlite3.Row) -> workflow.ActionExecution:
    """Recheck exact duplicates and create one approved Opportunity."""
    payload = dict(json.loads(str(row["payload_json"]))["opportunity"])
    account_id, name = str(payload["AccountId"]), str(payload["Name"])
    duplicate = next((item for item in gateway.open_opportunities(account_id)
                      if item.name.casefold() == name.casefold()), None)
    if duplicate:
        workflow._finish_action(
            conn, str(row["id"]), workflow.CampaignActionState.COMPLETE)
        return workflow.ActionExecution(
            workflow.CampaignActionState.COMPLETE,
            f"{name} is already in Salesforce: {duplicate.link}", already_present=1)
    workflow._mark_external_write_started(conn, str(row["id"]))
    result = gateway.create_opportunity(payload)
    if not result.success or not result.record_id:
        raise ValueError(result.error or "Salesforce returned no Opportunity ID")
    validate_record_id(result.record_id, "Opportunity")
    created = next((item for item in gateway.open_opportunities(account_id)
                    if item.record_id == result.record_id), None)
    if created is None or created.name != name or created.account_id != account_id:
        raise ValueError("created Opportunity could not be verified by exact readback")
    with conn:
        conn.execute(
            """UPDATE crm_action_items SET state='opportunity_created',salesforce_id=?
               WHERE action_id=?""", (result.record_id, row["id"]))
    workflow._finish_action(
        conn, str(row["id"]), workflow.CampaignActionState.COMPLETE)
    return workflow.ActionExecution(
        workflow.CampaignActionState.COMPLETE,
        f"Created {name} in Salesforce: {created.link}", added=1)


def confirm_lead_enrichment(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        row: sqlite3.Row) -> workflow.ActionExecution:
    """Recheck identity/concurrency, PATCH allowlisted fields once, and read back."""
    payload = json.loads(str(row["payload_json"]))
    lead_id = validate_record_id(str(payload["lead_id"]), "Lead")
    delta = dict(payload["delta"])
    before = gateway.lead_enrichment_snapshot(lead_id)
    if (before.company.casefold() != str(payload["company"]).casefold()
            or before.email.casefold() != str(payload["email"]).casefold()):
        raise ValueError("Salesforce Lead identity changed after preview")
    if before.system_modstamp != str(payload["system_modstamp"]):
        raise ValueError("Salesforce Lead changed after preview")
    workflow._mark_external_write_started(conn, str(row["id"]))
    action_id = str(row["id"])
    note_body = str(delta.get("Description") or "")
    task_body = _audit_task_description(action_id, "updated", list(delta))
    audit = gateway.enrich_lead_with_audit_bundle(
        lead_id, delta, str(payload["system_modstamp"]), action_id,
        note_body, task_body, _activity_date())
    if not audit.success:
        raise SalesforceCompositeRolledBack(
            audit.error or "Salesforce returned no Lead audit IDs")
    after = gateway.lead_enrichment_snapshot(lead_id)
    for key, value in delta.items():
        actual = after.values.get(key)
        if key == "Number_of_Students__c":
            if actual is None or float(actual) != float(value):
                raise ValueError("updated Lead enrollment did not match the preview")
        elif str(actual or "") != str(value):
            raise ValueError(f"updated Lead field {key} did not match the preview")
    if not gateway.verify_lead_audit_bundle(
            lead_id, action_id, note_body, task_body, audit):
        raise ValueError("created Salesforce Lead audit trail could not be verified")
    with conn:
        conn.execute(
            """UPDATE crm_action_items SET state='lead_enriched',salesforce_id=?
               WHERE action_id=?""", (lead_id, row["id"]))
    workflow._finish_action(
        conn, str(row["id"]), workflow.CampaignActionState.COMPLETE)
    return workflow.ActionExecution(
        workflow.CampaignActionState.COMPLETE,
        f"Updated the verified details in Salesforce: {after.link}", added=1)


def prepare_lead_audit_repair(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        lead_link: str) -> workflow.PreparedAction:
    """Prepare missing visible Note/Activity records for one completed Grant update."""
    workflow._validate_context(workspace, channel, thread_ts, requester)
    _sobject, lead_id = parse_record_link(lead_link, {"Lead"})
    prior = conn.execute(
        """SELECT a.*,i.salesforce_id
             FROM crm_actions a JOIN crm_action_items i ON i.action_id=a.id
            WHERE a.action_type='enrich_existing_lead' AND a.state='complete'
              AND i.state='lead_enriched' AND i.salesforce_id=?
              AND a.workspace=? AND a.channel=? AND a.thread_ts=? AND a.requested_by=?
            ORDER BY a.committed_at DESC LIMIT 1""",
        (lead_id, workspace, channel, thread_ts, requester)).fetchone()
    if prior is None:
        raise ValueError("no completed Grant Lead update was found in this thread")
    source_action = str(prior["id"])
    existing = gateway.lead_audit_snapshot(lead_id, source_action)
    if existing.complete:
        raise ValueError("the visible Salesforce Note and Activity already exist")
    if existing.partial:
        raise ValueError("the Salesforce audit trail is partial and requires reconciliation")
    payload = json.loads(str(prior["payload_json"]))
    snapshot = gateway.lead_enrichment_snapshot(lead_id)
    if (snapshot.company.casefold() != str(payload["company"]).casefold()
            or snapshot.email.casefold() != str(payload["email"]).casefold()):
        raise ValueError("Salesforce Lead no longer matches the completed Grant update")
    delta = dict(payload["delta"])
    note_body = str(delta.get("Description") or "")
    fields = [key for key in delta if key != "Description"]
    task_body = _audit_task_description(
        source_action, "updated", fields, str(prior["committed_at"] or ""))
    action_id = str(uuid.uuid4())
    plan = workflow.MemberPlan(
        None, f"lead:{lead_id}", snapshot.company, str(snapshot.values.get("State") or ""),
        "repair_lead_audit", note=json.dumps({"source_action_id": source_action}))
    stored_id, nonce, expires = workflow._store_action(
        conn, "repair_lead_audit", workspace, channel, thread_ts, requester,
        {"lead_id": lead_id, "source_action_id": source_action,
         "company": snapshot.company, "email": snapshot.email,
         "note_body": note_body, "task_description": task_body,
         "activity_date": _activity_date()}, plans=[plan], action_id=action_id)
    field_text = ", ".join(sorted(fields)) or "the verified research details"
    preview = (
        f"Add the missing Salesforce audit trail for {snapshot.company}?\n"
        "• Add one visible Salesforce Note with Grant’s verified sources\n"
        f"• Add one completed system Activity documenting: {field_text}\n"
        "• The Activity will explicitly say no customer outreach occurred\n"
        "No Lead fields, Campaigns, Campaign Members, or Opportunities will change."
    )
    return workflow.PreparedAction(stored_id, nonce, preview, expires)


def confirm_lead_audit_repair(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        row: sqlite3.Row) -> workflow.ActionExecution:
    """Create and verify only the missing Enhanced Note/link and system Task."""
    payload = json.loads(str(row["payload_json"]))
    lead_id = validate_record_id(str(payload["lead_id"]), "Lead")
    source_action = str(payload["source_action_id"])
    snapshot = gateway.lead_enrichment_snapshot(lead_id)
    if (snapshot.company.casefold() != str(payload["company"]).casefold()
            or snapshot.email.casefold() != str(payload["email"]).casefold()):
        raise ValueError("Salesforce Lead identity changed after the preview")
    existing = gateway.lead_audit_snapshot(lead_id, source_action)
    if existing.complete:
        workflow._finish_action(
            conn, str(row["id"]), workflow.CampaignActionState.COMPLETE)
        return workflow.ActionExecution(
            workflow.CampaignActionState.COMPLETE,
            f"The Salesforce Note and Activity are already present: {snapshot.link}",
            already_present=1)
    if existing.partial:
        raise ValueError("the Salesforce audit trail is partial and requires reconciliation")
    workflow._mark_external_write_started(conn, str(row["id"]))
    note_body = str(payload["note_body"])
    task_body = str(payload["task_description"])
    audit = gateway.create_lead_audit_bundle(
        lead_id, source_action, note_body, task_body, str(payload["activity_date"]))
    if not audit.success:
        raise SalesforceCompositeRolledBack(
            audit.error or "Salesforce returned no audit record IDs")
    if not gateway.verify_lead_audit_bundle(
            lead_id, source_action, note_body, task_body, audit):
        raise ValueError("Salesforce Lead audit trail could not be verified")
    with conn:
        conn.execute(
            """UPDATE crm_action_items SET state='audit_repaired',salesforce_id=?
               WHERE action_id=?""", (lead_id, row["id"]))
    workflow._finish_action(
        conn, str(row["id"]), workflow.CampaignActionState.COMPLETE)
    return workflow.ActionExecution(
        workflow.CampaignActionState.COMPLETE,
        f"Added the Salesforce Note and Activity: {snapshot.link}", added=1)
