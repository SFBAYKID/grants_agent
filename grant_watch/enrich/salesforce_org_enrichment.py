"""Contact-independent Salesforce enrichment for one exact organization Lead.

This workflow fills only blank organization and research fields. It never creates a
person, guesses an email, or selects among multiple Salesforce records.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import requests

from .. import db
from ..models import VerificationStatus
from . import finder
from . import salesforce_campaigns as workflow
from .organization_profile import OrganizationProfile, fetch_profile
from .salesforce_campaign_gateway import SalesforceCampaignGateway, parse_record_link


def _profile(
    conn: sqlite3.Connection, lead_id: int, company: str, state: str
) -> OrganizationProfile:
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
        return OrganizationProfile(website=f"https://{domain}/", source_url=source)


def _research_summary(
    company: str, funding_url: str, profile: OrganizationProfile
) -> str:
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
    address = ", ".join(
        filter(
            None,
            (
                profile.street,
                profile.city,
                profile.state,
                profile.postal_code,
                profile.country,
            ),
        )
    )
    if address:
        lines.append(f"Address: {address}")
    lines.extend(
        (
            "Contact status: No verified email is required to add these organization details.",
            "No customer outreach was performed.",
        )
    )
    return "\n".join(lines)


def prepare_organization_lead_enrichment(
    conn: sqlite3.Connection,
    gateway: SalesforceCampaignGateway,
    workspace: str,
    channel: str,
    thread_ts: str,
    requester: str,
    grant_lead_id: int,
    lead_link: str,
) -> workflow.PreparedAction:
    """Freeze a blank-only preview for one exact same-organization Salesforce Lead."""
    workflow._validate_context(workspace, channel, thread_ts, requester)
    _sobject, salesforce_id = parse_record_link(lead_link, {"Lead"})
    row = db.get_lead(conn, grant_lead_id)
    if row is None:
        raise ValueError("Grant lead is stale or unknown")
    company = str(row["entity_name"] or "").strip()
    state = str(row["state"] or "").strip().upper()
    funding_url = str(row["current_event_source_url"] or "").strip()
    if (
        str(row["current_event_verification_status"] or "")
        != VerificationStatus.VERIFIED.value
        or not funding_url
    ):
        raise ValueError("a verified current funding source is required")
    snapshot = gateway.lead_enrichment_snapshot(salesforce_id)
    if snapshot.company.casefold() != company.casefold():
        raise ValueError("Salesforce Lead does not match the Grant organization")
    profile = _profile(conn, grant_lead_id, company, state)
    entity_text = f"{row['entity_type'] or ''} {company}".lower()
    industry = (
        "K-12 Schools"
        if any(word in entity_text for word in ("school", "district", "k-12"))
        else ""
    )
    desired: dict[str, object] = {
        "Website": profile.website,
        "Phone": profile.main_phone,
        "Street": profile.street,
        "City": profile.city,
        "State": profile.state or state,
        "PostalCode": profile.postal_code,
        "Country": profile.country,
        "Industry": industry,
        "LinkedIn__c": profile.linkedin_url,
    }
    if row["enrollment"] is not None:
        desired["Number_of_Students__c"] = int(row["enrollment"])
    delta = {
        key: value
        for key, value in desired.items()
        if value not in (None, "") and snapshot.values.get(key) in (None, "")
    }
    summary = _research_summary(company, funding_url, profile)
    existing = str(snapshot.values.get("Description") or "").strip()
    if summary not in existing:
        delta["Description"] = f"{existing}\n\n{summary}".strip()
    if not delta:
        raise ValueError(
            "Salesforce Lead already contains every verified organization field"
        )
    action_id = str(uuid.uuid4())
    plan = workflow.MemberPlan(
        grant_lead_id,
        str(row["canonical_entity_key"] or company.lower()),
        company,
        state,
        "enrich_existing_lead",
        proposed_lead=delta,
        note=json.dumps(
            {
                "salesforce_id": salesforce_id,
                "system_modstamp": snapshot.system_modstamp,
            }
        ),
    )
    stored_id, nonce, expires = workflow._store_action(
        conn,
        "enrich_existing_lead",
        workspace,
        channel,
        thread_ts,
        requester,
        {
            "lead_id": salesforce_id,
            "delta": delta,
            "system_modstamp": snapshot.system_modstamp,
            "company": company,
            "email": snapshot.email,
        },
        plans=[plan],
        action_id=action_id,
    )
    labels = {
        "PostalCode": "Postal code",
        "LinkedIn__c": "LinkedIn",
        "Number_of_Students__c": "Students",
    }
    lines = [
        f"Fill these blank fields on the existing Salesforce Lead for {company}?",
        *(
            f"• {labels.get(key, key)}: {value}"
            for key, value in delta.items()
            if key != "Description"
        ),
        "• Replace the internal-only notes with a salesperson-readable research summary",
        "• Add a visible Salesforce Note with the same verified sources",
        "• Add a completed system Activity describing exactly what Grant updated",
        "No person, email, Campaign, Opportunity, or additional Lead will be created.",
    ]
    return workflow.PreparedAction(stored_id, nonce, "\n".join(lines), expires)
