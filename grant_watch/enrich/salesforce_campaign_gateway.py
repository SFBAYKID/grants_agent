"""Least-privilege Salesforce HTTP gateway for Campaign create operations.

This module owns the separate writer credentials and exposes GET plus an explicit
create allowlist. It intentionally contains no update or delete request primitive.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any  # Salesforce REST response JSON is runtime-shaped.
from urllib.parse import urlparse

import requests

API_VERSION = os.environ.get("SALESFORCE_API_VERSION", "v60.0")
MAX_ACTION_ORGANIZATIONS = 200
MEMBER_STATUS = "Identified by Grant"
_ALLOWED_CREATE_OBJECTS = {"Campaign", "CampaignMemberStatus", "Lead", "CampaignMember"}
_ID_PREFIXES = {
    "Campaign": "701",
    "Lead": "00Q",
    "Contact": "003",
    "Account": "001",
    "Opportunity": "006",
    "User": "005",
}


@dataclass(frozen=True)
class SalesforceRecordRef:
    """Validated Salesforce record used in a campaign preview."""

    sobject: str
    record_id: str
    name: str
    link: str
    company: str = ""
    state: str = ""



@dataclass(frozen=True)
class CreateResult:
    """One Salesforce create result aligned with its submitted payload."""

    success: bool
    record_id: str = ""
    error: str = ""



@dataclass
class _TokenCache:
    """Writer-token cache isolated from the reader credentials."""

    token: str = ""
    instance_url: str = ""
    expires_at: float = 0.0
    credential_scope: str = ""


_TOKEN_CACHE = _TokenCache()


def _soql_literal(value: str) -> str:
    """Escape a user string for a quoted SOQL literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _configured_host() -> str:
    """Return the exact writer-org hostname allowed in pasted Salesforce links."""
    raw = os.environ.get("SALESFORCE_WRITE_MY_DOMAIN_URL", "")
    return (urlparse(raw).hostname or "").lower()


def validate_record_id(record_id: str, expected_sobject: str) -> str:
    """Validate a 15/18-character Salesforce ID and its object prefix."""
    clean = record_id.strip()
    expected_prefix = _ID_PREFIXES.get(expected_sobject)
    if expected_prefix is None:
        raise ValueError(f"unsupported Salesforce object '{expected_sobject}'")
    if len(clean) not in (15, 18) or not clean.isalnum() or not clean.startswith(expected_prefix):
        raise ValueError(f"not a valid {expected_sobject} Salesforce ID")
    return clean


def parse_record_link(link: str, allowed_sobjects: set[str]) -> tuple[str, str]:
    """Validate hostname, Lightning object path, and Salesforce record prefix."""
    parsed = urlparse(link.strip())
    if not parsed.hostname or parsed.hostname.lower() != _configured_host():
        raise ValueError("Salesforce link is not from the configured Salesforce org")
    match = re.search(
        r"/lightning/r/([A-Za-z][A-Za-z0-9_]*)/([A-Za-z0-9]{15,18})(?:/|$)",
        parsed.path,
    )
    if match is None:
        raise ValueError("Salesforce link is not a Lightning record link")
    sobject, record_id = match.group(1), match.group(2)
    if sobject not in allowed_sobjects:
        raise ValueError(f"a {sobject} record cannot be used here")
    return sobject, validate_record_id(record_id, sobject)


class SalesforceCampaignGateway:
    """Least-privilege Salesforce reader/create client for Campaign operations."""

    def _auth(self, force: bool = False) -> tuple[str, str]:
        """Authenticate with the dedicated writer Connected App."""
        now = time.time()
        domain = os.environ["SALESFORCE_WRITE_MY_DOMAIN_URL"].rstrip("/")
        client_id = os.environ["SALESFORCE_WRITE_CLIENT_ID"]
        credential_scope = f"{domain}|{client_id}"
        if (not force and _TOKEN_CACHE.token and _TOKEN_CACHE.expires_at > now
                and _TOKEN_CACHE.credential_scope == credential_scope):
            return _TOKEN_CACHE.token, _TOKEN_CACHE.instance_url
        response = requests.post(
            f"{domain}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": os.environ["SALESFORCE_WRITE_CLIENT_SECRET"],
            },
            timeout=20,
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()  # Salesforce OAuth JSON is runtime-shaped
        _TOKEN_CACHE.token = str(body["access_token"])
        _TOKEN_CACHE.instance_url = str(body.get("instance_url") or domain).rstrip("/")
        _TOKEN_CACHE.expires_at = now + 25 * 60
        _TOKEN_CACHE.credential_scope = credential_scope
        return _TOKEN_CACHE.token, _TOKEN_CACHE.instance_url

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """Issue a read request with writer credentials for pre-create validation."""
        token, instance = self._auth()
        response = requests.get(
            f"{instance}/services/data/{API_VERSION}/{path}", params=params or {},
            headers={"Authorization": f"Bearer {token}"}, timeout=20,
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]  # third-party JSON

    def _create_one(self, sobject: str, payload: dict[str, object]) -> CreateResult:
        """Create one record on the explicit object allowlist."""
        if sobject not in _ALLOWED_CREATE_OBJECTS:
            raise ValueError(f"Salesforce create forbidden for {sobject}")
        token, instance = self._auth()
        response = requests.post(
            f"{instance}/services/data/{API_VERSION}/sobjects/{sobject}",
            json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=20,
        )
        if response.status_code not in (200, 201):
            return CreateResult(False, error=f"HTTP {response.status_code}: {response.text[:200]}")
        body: dict[str, Any] = response.json()  # Salesforce create JSON is runtime-shaped
        return CreateResult(bool(body.get("success", True)), str(body.get("id") or ""))

    def _create_many(self, sobject: str,
                     payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Create up to 200 records with per-record results and allOrNone disabled."""
        if sobject not in _ALLOWED_CREATE_OBJECTS:
            raise ValueError(f"Salesforce create forbidden for {sobject}")
        if len(payloads) > MAX_ACTION_ORGANIZATIONS:
            raise ValueError("Salesforce collection exceeds 200 records")
        if not payloads:
            return []
        token, instance = self._auth()
        records = [{"attributes": {"type": sobject}, **payload} for payload in payloads]
        response = requests.post(
            f"{instance}/services/data/{API_VERSION}/composite/sobjects",
            params={"allOrNone": "false"}, json={"records": records},
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        response.raise_for_status()
        body: list[dict[str, Any]] = response.json()  # third-party collection result
        results: list[CreateResult] = []
        for item in body:
            errors = item.get("errors") or []
            error = "; ".join(str(err.get("message") or err) for err in errors)
            results.append(CreateResult(
                bool(item.get("success")), str(item.get("id") or ""), error,
            ))
        return results

    def lightning_link(self, sobject: str, record_id: str) -> str:
        """Build a writer-org Lightning record link."""
        _, instance = self._auth()
        return f"{instance}/lightning/r/{sobject}/{record_id}/view"

    def campaign_picklists(self) -> tuple[set[str], set[str]]:
        """Return currently valid Campaign Type and Status picklist values."""
        body = self._get("sobjects/Campaign/describe")
        values: dict[str, set[str]] = {"Type": set(), "Status": set()}
        for field in body.get("fields") or []:
            name = str(field.get("name") or "")
            if name in values:
                values[name] = {
                    str(item.get("value")) for item in field.get("picklistValues") or []
                    if item.get("active")
                }
        return values["Type"], values["Status"]

    def find_active_user_by_email(self, email: str) -> list[SalesforceRecordRef]:
        """Resolve an owner only by exact active-user email; never guess by name."""
        if not email.strip():
            return []
        literal = _soql_literal(email.strip().lower())
        soql = (
            "SELECT Id,Name,Email FROM User "
            f"WHERE IsActive=true AND Email='{literal}' LIMIT 2"
        )
        records = self._get("query", {"q": soql}).get("records") or []
        return [SalesforceRecordRef(
            "User", str(record["Id"]), str(record.get("Name") or ""),
            self.lightning_link("User", str(record["Id"])),
        ) for record in records]

    def search_campaigns(self, name: str) -> list[SalesforceRecordRef]:
        """Return exact/contains Campaign candidates for human selection."""
        literal = _soql_literal(name.strip())
        soql = (
            "SELECT Id,Name,Status,Type,IsActive,Owner.Name FROM Campaign "
            f"WHERE Name LIKE '%{literal}%' ORDER BY LastModifiedDate DESC LIMIT 20"
        )
        records = self._get("query", {"q": soql}).get("records") or []
        return [SalesforceRecordRef(
            "Campaign", str(record["Id"]), str(record.get("Name") or ""),
            self.lightning_link("Campaign", str(record["Id"])),
        ) for record in records]

    def get_record(self, sobject: str, record_id: str) -> SalesforceRecordRef:
        """Read back a Campaign, Lead, or Contact before showing confirmation."""
        validate_record_id(record_id, sobject)
        fields = {
            "Campaign": "Id,Name",
            "Lead": "Id,Name,Company,State",
            "Contact": "Id,Name,MailingState,Account.Name",
        }[sobject]
        body = self._get(f"sobjects/{sobject}/{record_id}", {"fields": fields})
        account = body.get("Account") or {}
        return SalesforceRecordRef(
            sobject=sobject, record_id=record_id, name=str(body.get("Name") or ""),
            link=self.lightning_link(sobject, record_id),
            company=str(body.get("Company") or account.get("Name") or ""),
            state=str(body.get("State") or body.get("MailingState") or ""),
        )

    def find_people(self, entity_name: str, state: str) -> list[SalesforceRecordRef]:
        """Find exact-company Leads and Account Contacts; never auto-select fuzzy rows."""
        literal = _soql_literal(entity_name.strip())
        state_filter = f" AND State='{_soql_literal(state.upper())}'" if state else ""
        lead_soql = (
            "SELECT Id,Name,Company,State FROM Lead "
            f"WHERE Company='{literal}'{state_filter} LIMIT 20"
        )
        contact_state = (
            f" AND MailingState='{_soql_literal(state.upper())}'" if state else ""
        )
        contact_soql = (
            "SELECT Id,Name,MailingState,Account.Name FROM Contact "
            f"WHERE Account.Name='{literal}'{contact_state} LIMIT 20"
        )
        records: list[tuple[str, dict[str, Any]]] = []
        records.extend(("Lead", item) for item in self._get(
            "query", {"q": lead_soql}).get("records") or [])
        records.extend(("Contact", item) for item in self._get(
            "query", {"q": contact_soql}).get("records") or [])
        refs: list[SalesforceRecordRef] = []
        for sobject, record in records:
            account = record.get("Account") or {}
            record_id = str(record["Id"])
            refs.append(SalesforceRecordRef(
                sobject, record_id, str(record.get("Name") or ""),
                self.lightning_link(sobject, record_id),
                company=str(record.get("Company") or account.get("Name") or ""),
                state=str(record.get("State") or record.get("MailingState") or ""),
            ))
        return refs

    def member_status_exists(self, campaign_id: str) -> bool:
        """Return whether the honest non-response member status is configured."""
        literal = _soql_literal(MEMBER_STATUS)
        soql = (
            "SELECT Id,HasResponded FROM CampaignMemberStatus "
            f"WHERE CampaignId='{campaign_id}' AND Label='{literal}' LIMIT 2"
        )
        records = self._get("query", {"q": soql}).get("records") or []
        if not records:
            return False
        if any(bool(record.get("HasResponded")) for record in records):
            raise ValueError(f"Campaign status '{MEMBER_STATUS}' incorrectly marks responded")
        return True

    def existing_members(self, campaign_id: str,
                         record_ids: list[str]) -> set[str]:
        """Return Lead/Contact IDs already present in the selected Campaign."""
        if not record_ids:
            return set()
        quoted = ",".join(f"'{_soql_literal(item)}'" for item in record_ids)
        soql = (
            "SELECT Id,LeadId,ContactId FROM CampaignMember "
            f"WHERE CampaignId='{campaign_id}' AND (LeadId IN ({quoted}) "
            f"OR ContactId IN ({quoted}))"
        )
        records = self._get("query", {"q": soql}).get("records") or []
        return {str(record.get("LeadId") or record.get("ContactId")) for record in records}

    def create_campaign(self, payload: dict[str, object]) -> CreateResult:
        """Create one Campaign."""
        return self._create_one("Campaign", payload)

    def create_member_status(self, campaign_id: str) -> CreateResult:
        """Create the disclosed non-response status without changing existing defaults."""
        return self._create_one("CampaignMemberStatus", {
            "CampaignId": campaign_id,
            "Label": MEMBER_STATUS,
            "HasResponded": False,
        })

    def create_leads(self, payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Create approved Lead records through the collection endpoint."""
        return self._create_many("Lead", payloads)

    def create_members(self, payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Create approved Campaign Members with per-record results."""
        return self._create_many("CampaignMember", payloads)
