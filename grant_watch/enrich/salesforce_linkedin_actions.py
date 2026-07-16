"""Human-approved Salesforce actions for LinkedIn-only person candidates.

The workflow preserves the absence of an email, prevents organization-only fallback,
and either creates one singular person Lead or repairs one exact Grant-created
organization placeholder. Every write includes an Enhanced Note and administrative
Task in the same all-or-none Salesforce transaction.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass

import requests

from .. import db, linkedin_candidates
from . import finder
from . import salesforce_campaigns as workflow
from .organization_profile import OrganizationProfile, fetch_profile
from .salesforce_campaign_gateway import (
    SalesforceCampaignGateway,
    SalesforceCompositeRolledBack,
    validate_record_id,
)
from .salesforce_record_actions import (
    _activity_date,
    _audit_task_description,
    duplicate_organization,
)


@dataclass(frozen=True)
class LinkedInPersonDraft:
    """One exact no-email person and verified organization facts for Salesforce."""

    candidate_id: str
    grant_lead_id: int
    name: str
    title: str
    profile_url: str
    company: str
    state: str
    funding_url: str
    evidence_excerpt: str
    organization: OrganizationProfile
    enrollment: int | None
    industry: str

    def person_fields(self) -> dict[str, object]:
        """Split only the final name token and retain nicknames verbatim in FirstName."""
        parts = self.name.split()
        if not parts:
            raise ValueError("LinkedIn candidate name is empty")
        last_name = parts[-1]
        if not re.fullmatch(r"[A-Za-z][A-Za-z'’.-]*", last_name):
            return {"LastName": self.name}
        result: dict[str, object] = {"LastName": last_name}
        if len(parts) > 1:
            result["FirstName"] = " ".join(parts[:-1])
        return result

    def research_note(self, action_id: str, requester: str) -> str:
        """Describe exact evidence and explicitly preserve the missing-email truth."""
        lines = [
            f"Grant LinkedIn candidate evidence: {self.profile_url}",
            f"Search-result excerpt: {self.evidence_excerpt}",
            "No email was found or verified; Email remains blank.",
            f"Funding evidence: {self.funding_url}",
            f"Grant lead {self.grant_lead_id}. Candidate {self.candidate_id}.",
            f"Action {action_id}. Requested by Slack user {requester}.",
        ]
        if self.organization.source_url:
            lines.append(f"Official organization source: {self.organization.source_url}")
        return "\n".join(lines)

    def desired_fields(self, action_id: str, requester: str) -> dict[str, object]:
        """Return supported person plus verified organization fields, never guessed data."""
        result = self.person_fields()
        result.update({
            "Company": self.company, "Status": "New", "LeadSource": "Other",
            "LinkedIn__c": self.profile_url,
            "Description": self.research_note(action_id, requester),
        })
        if self.title:
            result["Title"] = self.title
        for key, value in (
            ("Phone", self.organization.main_phone),
            ("Website", self.organization.website),
            ("Street", self.organization.street),
            ("City", self.organization.city),
            ("State", self.organization.state or self.state),
            ("PostalCode", self.organization.postal_code),
            ("Country", self.organization.country),
            ("Industry", self.industry),
        ):
            if value:
                result[key] = value
        if self.enrollment is not None:
            result["Number_of_Students__c"] = self.enrollment
        return result


def _draft(conn: sqlite3.Connection, candidate: linkedin_candidates.LinkedInCandidate
           ) -> LinkedInPersonDraft:
    """Join a candidate to its verified funding lead and available official profile."""
    row = db.get_lead(conn, candidate.lead_id)
    if row is None or str(row["entity_name"] or "").casefold() != candidate.organization.casefold():
        raise ValueError("LinkedIn candidate no longer matches the Grant lead")
    funding_url = str(row["current_event_source_url"] or "").strip()
    if (str(row["current_event_verification_status"] or "") != "verified"
            or not funding_url):
        raise ValueError("a verified current funding source is required")
    site = conn.execute(
        """SELECT official_domain,source_url FROM contacts
             WHERE lead_id=? AND TRIM(COALESCE(official_domain,''))!=''
             ORDER BY CASE contact_status WHEN 'verified' THEN 0 ELSE 1 END,id LIMIT 1""",
        (candidate.lead_id,),
    ).fetchone()
    domain = str(site["official_domain"] or "").strip() if site else ""
    site_source = str(site["source_url"] or "").strip() if site else ""
    if domain:
        try:
            organization = fetch_profile(candidate.organization, domain, site_source)
        except (KeyError, ValueError, RuntimeError, requests.RequestException):
            organization = OrganizationProfile(
                website=f"https://{domain}/", source_url=site_source)
    else:
        official = finder.find_official_site(
            candidate.organization, str(row["state"] or ""))
        if official is None:
            organization = OrganizationProfile()
        else:
            try:
                organization = fetch_profile(
                    candidate.organization, official.domain, official.url)
            except (KeyError, ValueError, RuntimeError, requests.RequestException):
                organization = OrganizationProfile(
                    website=f"https://{official.domain}/", source_url=official.url)
    entity_text = f"{row['entity_type'] or ''} {candidate.organization}".lower()
    industry = "K-12 Schools" if any(
        word in entity_text for word in ("school", "district", "k-12")) else ""
    enrollment = int(row["enrollment"]) if row["enrollment"] is not None else None
    return LinkedInPersonDraft(
        candidate.candidate_id, candidate.lead_id, candidate.person_name,
        candidate.title, candidate.profile_url, candidate.organization,
        str(row["state"] or "").upper(), funding_url, candidate.evidence_excerpt,
        organization, enrollment, industry,
    )


def _matching_placeholder(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        company: str, state: str
        ) -> str:
    """Return one exact Grant-created organization placeholder, or fail closed."""
    matches = duplicate_organization(company, state)
    lead_ids = [item.record_id for item in matches
                if item.sobject == "Lead" and item.company.casefold() == company.casefold()
                and (not state or not item.state or item.state.upper() == state)]
    if len(lead_ids) > 1:
        raise ValueError("Salesforce has multiple matching Leads; no record was selected")
    if not lead_ids:
        if matches:
            links = ", ".join(item.link for item in matches[:3])
            raise ValueError(f"Salesforce already has a possible matching record: {links}")
        return ""
    snapshot = gateway.linkedin_person_lead_snapshot(lead_ids[0])
    placeholder = (
        snapshot.company.casefold() == company.casefold()
        and snapshot.last_name.casefold() == company.casefold()
        and not any((snapshot.first_name, snapshot.email, snapshot.title,
                     snapshot.linkedin_url))
        and "Created by Grant as an organization-only Lead" in snapshot.description
    )
    if not placeholder:
        raise ValueError(f"Salesforce already has a person Lead: {snapshot.link}")
    marker = re.search(r"\bAction ([0-9a-f-]{36})\b", snapshot.description)
    if marker is None:
        raise ValueError("The organization Lead is missing its Grant audit marker")
    ledger = conn.execute(
        """SELECT a.state,i.state AS item_state,i.salesforce_id
             FROM crm_actions a JOIN crm_action_items i ON i.action_id=a.id
            WHERE a.id=? AND a.action_type='create_organization_lead'""",
        (marker.group(1),),
    ).fetchone()
    if (ledger is None or str(ledger["state"]) != "complete"
            or str(ledger["item_state"]) != "lead_created"
            or str(ledger["salesforce_id"] or "") != snapshot.record_id):
        raise ValueError("The existing Grant Lead must be reconciled before it is updated")
    return snapshot.record_id


def prepare_linkedin_person(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        candidate_id: str) -> workflow.PreparedAction:
    """Freeze a person-specific create or exact placeholder-repair preview."""
    workflow._validate_context(workspace, channel, thread_ts, requester)
    candidate = linkedin_candidates.get_candidate(
        conn, candidate_id, workspace, channel, thread_ts, requester)
    draft = _draft(conn, candidate)
    action_id = str(uuid.uuid4())
    person = draft.person_fields()
    duplicates = gateway.exact_linkedin_person_leads(
        draft.profile_url, draft.company, str(person["LastName"]))
    if duplicates:
        links = ", ".join(item.link for item in duplicates[:3])
        raise ValueError(f"That LinkedIn person may already be in Salesforce: {links}")
    placeholder_id = _matching_placeholder(conn, gateway, draft.company, draft.state)
    desired = draft.desired_fields(action_id, requester)
    operation = "create_linkedin_person_lead"
    payload: dict[str, object] = {"lead": desired, "candidate_id": candidate_id}
    preview_verb = "Create"
    if placeholder_id:
        snapshot = gateway.linkedin_person_lead_snapshot(placeholder_id)
        enrichment = gateway.lead_enrichment_snapshot(placeholder_id)
        mutable = {"FirstName", "LastName", "Title", "LinkedIn__c", "Description"}
        delta = {key: value for key, value in desired.items()
                 if key in mutable or (key in enrichment.values
                                       and enrichment.values[key] in (None, ""))}
        prior = snapshot.description.strip()
        delta["Description"] = f"{prior}\n\n{desired['Description']}".strip()
        operation = "attach_linkedin_person_to_lead"
        payload = {
            "lead_id": placeholder_id, "delta": delta,
            "system_modstamp": snapshot.system_modstamp,
            "company": draft.company, "candidate_id": candidate_id,
        }
        preview_verb = "Update the existing organization Lead for"
    plan = workflow.MemberPlan(
        draft.grant_lead_id, f"linkedin:{candidate_id}", draft.company, draft.state,
        operation, proposed_lead=desired,
        note=json.dumps({"candidate_id": candidate_id, "profile_url": draft.profile_url}))
    stored_id, nonce, expires = workflow._store_action(
        conn, operation, workspace, channel, thread_ts, requester, payload,
        plans=[plan], action_id=action_id)
    fields = desired if not placeholder_id else dict(payload["delta"])
    lines = [
        f"{preview_verb} {draft.name} in Salesforce?",
        f"• Organization: {draft.company}",
        f"• Role: {draft.title or 'not shown'}",
        f"• LinkedIn: {draft.profile_url}",
        "• Email: not found or verified; stays blank",
    ]
    labels = {"Number_of_Students__c": "Students", "LinkedIn__c": "LinkedIn"}
    hidden = {"Company", "FirstName", "LastName", "Title", "LinkedIn__c",
              "Description", "Status", "LeadSource"}
    lines.extend(f"• {labels.get(key, key)}: {value}"
                 for key, value in fields.items() if key not in hidden)
    lines.extend((
        "• Add a visible Salesforce Note with the exact research sources",
        "• Add a completed system Activity; no customer outreach will be recorded",
        "No Campaign, Campaign Member, Opportunity, or additional Lead will be created.",
    ))
    return workflow.PreparedAction(stored_id, nonce, "\n".join(lines), expires)


def _verify_identity(snapshot: object, company: str, fields: dict[str, object]) -> bool:
    """Compare the exact person identity fields after one create or update."""
    return bool(
        getattr(snapshot, "company").casefold() == company.casefold()
        and getattr(snapshot, "first_name") == str(fields.get("FirstName") or "")
        and getattr(snapshot, "last_name") == str(fields["LastName"])
        and getattr(snapshot, "title") == str(fields.get("Title") or "")
        and getattr(snapshot, "linkedin_url") == str(fields["LinkedIn__c"])
        and not getattr(snapshot, "email")
    )


def confirm_linkedin_person(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        row: sqlite3.Row) -> workflow.ActionExecution:
    """Execute one approved LinkedIn person create/update with exact readback."""
    payload = json.loads(str(row["payload_json"]))
    candidate = linkedin_candidates.get_candidate(
        conn, str(payload["candidate_id"]), str(row["workspace"]), str(row["channel"]),
        str(row["thread_ts"]), str(row["requested_by"]))
    draft = _draft(conn, candidate)
    action_id = str(row["id"])
    if row["action_type"] == "create_linkedin_person_lead":
        fields = dict(payload["lead"])
        if duplicate_organization(draft.company, draft.state):
            raise ValueError("Salesforce changed after preview; no duplicate Lead was created")
        if gateway.exact_linkedin_person_leads(
                draft.profile_url, draft.company, str(fields["LastName"])):
            raise ValueError("That LinkedIn person is already in Salesforce")
        workflow._mark_external_write_started(conn, action_id)
        note_body = str(fields["Description"])
        task_body = _audit_task_description(
            action_id, "created and populated", list(fields))
        result = gateway.create_linkedin_person_lead_with_audit_bundle(
            fields, action_id, note_body, task_body, _activity_date())
        if not result.success or not result.lead_id:
            raise SalesforceCompositeRolledBack(result.error or "Salesforce rolled back")
        lead_id = validate_record_id(result.lead_id, "Lead")
    else:
        lead_id = validate_record_id(str(payload["lead_id"]), "Lead")
        fields = dict(payload["delta"])
        before = gateway.linkedin_person_lead_snapshot(lead_id)
        if (before.company.casefold() != draft.company.casefold()
                or before.system_modstamp != str(payload["system_modstamp"])
                or before.last_name.casefold() != draft.company.casefold()
                or any((before.first_name, before.email, before.title,
                        before.linkedin_url))):
            raise ValueError("Salesforce Lead changed after preview")
        others = [item for item in gateway.exact_linkedin_person_leads(
            draft.profile_url, draft.company, str(fields["LastName"]))
                  if item.record_id != lead_id]
        if others:
            raise ValueError("That LinkedIn person is already in Salesforce")
        workflow._mark_external_write_started(conn, action_id)
        note_body = draft.research_note(action_id, str(row["requested_by"]))
        task_body = _audit_task_description(action_id, "updated", list(fields))
        result = gateway.attach_linkedin_person_with_audit_bundle(
            lead_id, fields, str(payload["system_modstamp"]), action_id,
            note_body, task_body, _activity_date())
        if not result.success:
            raise SalesforceCompositeRolledBack(result.error or "Salesforce rolled back")
    after = gateway.linkedin_person_lead_snapshot(lead_id)
    if not _verify_identity(after, draft.company, fields):
        raise ValueError("Salesforce Lead person fields did not match the approved preview")
    if not gateway.verify_lead_audit_bundle(
            lead_id, action_id, note_body, task_body, result):
        raise ValueError("Salesforce Lead audit trail could not be verified")
    linkedin_candidates.consume_candidate(conn, candidate.candidate_id, action_id)
    with conn:
        conn.execute(
            """UPDATE crm_action_items SET state='lead_created',salesforce_id=?
                 WHERE action_id=?""", (lead_id, action_id))
    workflow._finish_action(conn, action_id, workflow.CampaignActionState.COMPLETE)
    return workflow.ActionExecution(
        workflow.CampaignActionState.COMPLETE,
        f"Added {draft.name} to Salesforce: {after.link}", added=1)
