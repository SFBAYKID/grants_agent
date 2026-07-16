"""Contact-independent Salesforce enrichment for one exact organization Lead.

This workflow fills only blank organization and research fields. It never creates a
person, guesses an email, or selects among multiple Salesforce records.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid

import requests

from .. import db
from ..models import VerificationStatus
from ..presentation import display_entity_name
from . import finder
from . import salesforce
from . import salesforce_campaigns as workflow
from .organization_profile import OrganizationProfile, fetch_profile
from .salesforce_campaign_gateway import SalesforceCampaignGateway, parse_record_link
from .salesforce_record_actions import _audit_task_description, _verified_industry


def select_exact_lead(
        matches: list[salesforce.SFMatch], company: str, state: str
        ) -> salesforce.SFMatch:
    """Select one high-confidence Lead with exact organization and state evidence."""
    exact = [item for item in matches
             if item.sobject == "Lead" and item.confidence == "high"
             and item.company.casefold() == company.casefold()
             and bool(state) and item.state.upper() == state.upper()]
    if len(exact) != 1:
        raise ValueError("Salesforce must have one exact matching Lead with state evidence")
    return exact[0]


def _legacy_description_is_grant_owned(
        conn: sqlite3.Connection, salesforce_id: str, description: str) -> bool:
    """Allow replacement only for the exact reconciled legacy placeholder copy."""
    legacy_prefix = (
        "Created by Grant as an organization-only Lead. No individual contact or "
        "email was verified."
    )
    if not description.startswith(legacy_prefix):
        return False
    marker = re.search(r"\bAction ([0-9a-f-]{36})\b", description)
    if marker is None:
        return False
    row = conn.execute(
        """SELECT a.state,i.state AS item_state,i.salesforce_id
             FROM crm_actions a JOIN crm_action_items i ON i.action_id=a.id
            WHERE a.id=? AND a.action_type='create_organization_lead'""",
        (marker.group(1),),
    ).fetchone()
    return bool(
        row is not None and str(row["state"]) == "complete"
        and str(row["item_state"]) == "lead_created"
        and str(row["salesforce_id"] or "") == salesforce_id)


def _profile(conn: sqlite3.Connection, lead_id: int, company: str,
             state: str) -> OrganizationProfile:
    """Return code-verified official organization facts without needing a contact."""
    site = conn.execute(
        """SELECT official_domain,source_url FROM contacts
             WHERE lead_id=? AND TRIM(COALESCE(official_domain,''))!=''
             ORDER BY CASE contact_status WHEN 'verified' THEN 0 ELSE 1 END,id LIMIT 1""",
        (lead_id,),
    ).fetchone()
    domain = str(site["official_domain"] or "").strip() if site else ""
    source = str(site["source_url"] or "").strip() if site else ""
    if not domain:
        official = finder.find_official_site(company, state)
        if official is None:
            raise ValueError("the official organization website could not be verified")
        domain, source = official.domain, official.url
    try:
        return fetch_profile(company, domain, source or f"https://{domain}/")
    except (KeyError, ValueError, RuntimeError, requests.RequestException):
        return OrganizationProfile(
            website=f"https://{domain}/", source_url=source)


def _research_summary(company: str, funding_url: str,
                      profile: OrganizationProfile) -> str:
    """Build salesperson-readable research notes with source links and no internals."""
    lines = [
        f"Grant research summary for {company}",
        "",
        f"Funding record: {funding_url}",
    ]
    if profile.website:
        lines.append(f"Official website: {profile.website}")
    if profile.main_phone:
        lines.append(f"Main phone: {profile.main_phone}")
    address = ", ".join(filter(None, (
        profile.street, profile.city, profile.state, profile.postal_code,
        profile.country,
    )))
    if address:
        lines.append(f"Address: {address}")
    lines.extend((
        "Contact status: No verified email is required to add these organization details.",
        "No customer outreach was performed.",
    ))
    return "\n".join(lines)


def prepare_organization_lead_enrichment(
        conn: sqlite3.Connection, gateway: SalesforceCampaignGateway,
        workspace: str, channel: str, thread_ts: str, requester: str,
        grant_lead_id: int, lead_link: str) -> workflow.PreparedAction:
    """Freeze a blank-only preview for one exact same-organization Salesforce Lead."""
    workflow._validate_context(workspace, channel, thread_ts, requester)
    _sobject, salesforce_id = parse_record_link(lead_link, {"Lead"})
    row = db.get_lead(conn, grant_lead_id)
    if row is None:
        raise ValueError("Grant lead is stale or unknown")
    company = str(row["entity_name"] or "").strip()
    state = str(row["state"] or "").strip().upper()
    funding_url = str(row["current_event_source_url"] or "").strip()
    if (str(row["current_event_verification_status"] or "")
            != VerificationStatus.VERIFIED.value or not funding_url):
        raise ValueError("a verified current funding source is required")
    snapshot = gateway.lead_enrichment_snapshot(salesforce_id)
    if snapshot.company.casefold() != company.casefold():
        raise ValueError("Salesforce Lead does not match the Grant organization")
    profile = _profile(conn, grant_lead_id, company, state)
    industry = _verified_industry(row["entity_type"])
    desired: dict[str, object] = {
        "Website": profile.website, "Phone": profile.main_phone,
        "Street": profile.street, "City": profile.city,
        "State": profile.state or state, "PostalCode": profile.postal_code,
        "Country": profile.country, "Industry": industry,
    }
    if row["enrollment"] is not None:
        desired["Number_of_Students__c"] = int(row["enrollment"])
    delta = {key: value for key, value in desired.items()
             if value not in (None, "") and snapshot.values.get(key) in (None, "")}
    display_company = display_entity_name(company)
    summary = _research_summary(display_company, funding_url, profile)
    existing = str(snapshot.values.get("Description") or "").strip()
    replace_legacy = _legacy_description_is_grant_owned(
        conn, salesforce_id, existing)
    if summary not in existing:
        delta["Description"] = (
            summary if replace_legacy else f"{existing}\n\n{summary}".strip())
    if not delta:
        raise ValueError("Salesforce Lead already contains every verified organization field")
    action_id = str(uuid.uuid4())
    plan = workflow.MemberPlan(
        grant_lead_id, str(row["canonical_entity_key"] or company.lower()),
        company, state, "enrich_existing_lead", proposed_lead=delta,
        note=json.dumps({"salesforce_id": salesforce_id,
                         "system_modstamp": snapshot.system_modstamp}))
    task_description = _audit_task_description(action_id, "updated", list(delta))
    labels = {
        "Website": "Website", "Phone": "Phone", "Street": "Street",
        "City": "City", "State": "State", "PostalCode": "Postal code",
        "Country": "Country", "Industry": "Industry",
        "Number_of_Students__c": "Students",
    }
    lines = [
        f"Fill these blank fields on the existing Salesforce Lead for {display_company}?",
        *(f"• {labels.get(key, key)}: {value}" for key, value in delta.items()
          if key != "Description"),
        ("• Replace Grant’s internal-only placeholder description with the research notes below"
         if replace_legacy else
         "• Append the research notes below without removing existing description text"),
        "",
        "Research notes and visible Salesforce Note:",
        summary,
        "",
        "Completed system Activity:",
        task_description,
        "",
        "No person, email, Campaign, Opportunity, or additional Lead will be created.",
    ]
    preview = "\n".join(lines)
    stored_id, nonce, expires = workflow._store_action(
        conn, "enrich_existing_lead", workspace, channel, thread_ts, requester,
        {"lead_id": salesforce_id, "delta": delta,
         "system_modstamp": snapshot.system_modstamp,
         "company": company, "email": snapshot.email,
         "identity_state": state, "note_body": summary,
         "task_description": task_description, "approval_preview": preview},
        plans=[plan], action_id=action_id)
    return workflow.PreparedAction(stored_id, nonce, preview, expires)
