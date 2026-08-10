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

from .. import db

API_VERSION = os.environ.get("SALESFORCE_API_VERSION", "v60.0")
MAX_ACTION_ORGANIZATIONS = 200
MEMBER_STATUS = "Identified by Grant"
# Grant never creates Salesforce activity Tasks (Chase, 2026-07-18: "we don't use
# tasks — log it as a note"). Task is deliberately absent from the allowlist so a
# future bug cannot create one; the grant context is logged as a ContentNote.
_ALLOWED_CREATE_OBJECTS = {
    "Campaign",
    "CampaignMemberStatus",
    "Lead",
    "CampaignMember",
    "Note",
    "ContentNote",
    "ContentDocumentLink",
}
# THE ONLY Lead fields Grant may write into an EXISTING record, and only while they
# are EMPTY. This is the narrowest possible breach of the create-only rule, shaped so
# it cannot destroy anything: no field here is ever overwritten and nothing is ever
# cleared. Name, Company, OwnerId and Status are deliberately ABSENT — those are
# identity and workflow, and filling them would change what a record IS and who owns
# it, rather than what is known about it.
_ALLOWED_LEAD_FILL_FIELDS = frozenset(
    {
        "Street",
        "City",
        "State",
        "PostalCode",
        "Phone",
        "MobilePhone",
        "Email",
        "Title",
        "Website",
        "Industry",
        "Number_of_Students__c",
    }
)

_ID_PREFIXES = {
    "Campaign": "701",
    "Lead": "00Q",
    "Contact": "003",
    "Account": "001",
    "Opportunity": "006",
    "User": "005",
    "Organization": "00D",
    "Note": "002",
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
    account_id: str = ""


@dataclass(frozen=True)
class CreateResult:
    """One Salesforce create result aligned with its submitted payload."""

    success: bool
    record_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class SalesforceOrganizationIdentity:
    """Authoritative Salesforce organization identity for write-scope checks."""

    organization_id: str
    name: str
    is_sandbox: bool
    instance_name: str
    instance_url: str


@dataclass
class _TokenCache:
    """Writer-token cache isolated from the reader credentials."""

    token: str = ""
    instance_url: str = ""
    expires_at: float = 0.0
    credential_scope: str = ""


_TOKEN_CACHE = _TokenCache()
# Lead RecordType ids resolved by DeveloperName; small and stable per org.
_RECORD_TYPE_CACHE: dict[str, str] = {}


def _soql_literal(value: str) -> str:
    """Escape a user string for a quoted SOQL literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# Writer credentials fall back to the READER's when no separate writer app is
# configured (Chase, 2026-07-19): one Connected App for both read and write is a valid,
# common setup, so requiring duplicate SALESFORCE_WRITE_CLIENT_ID/SECRET/MY_DOMAIN_URL
# that just mirror the reader is needless env duplication. A distinct writer app is still
# supported — set the SALESFORCE_WRITE_* vars and they take precedence. The write-SAFETY
# vars (SALESFORCE_WRITE_ORG_ID and SALESFORCE_WRITE_EXPECT_SANDBOX) remain explicit and
# have no fallback — they must be set deliberately before any write.
def _write_client_id() -> str:
    """Writer OAuth client id, defaulting to the reader's when not separately set."""
    return (
        os.environ.get("SALESFORCE_WRITE_CLIENT_ID")
        or os.environ["SALESFORCE_CLIENT_ID"]
    )


def _write_client_secret() -> str:
    """Writer OAuth client secret, defaulting to the reader's when not separately set."""
    return (
        os.environ.get("SALESFORCE_WRITE_CLIENT_SECRET")
        or os.environ["SALESFORCE_CLIENT_SECRET"]
    )


def _write_my_domain() -> str:
    """Writer My Domain URL, defaulting to the reader's when not separately set."""
    return (
        os.environ.get("SALESFORCE_WRITE_MY_DOMAIN_URL")
        or os.environ["SALESFORCE_MY_DOMAIN_URL"]
    )


def _configured_hosts() -> frozenset[str]:
    """Every hostname that provably belongs to the configured writer org.

    Salesforce serves one org under two names, and its own UI hands the rep the
    second one: `<instance>.my.salesforce.com` is the My Domain / API host, while
    `<instance>.lightning.force.com` is what the address bar shows and what Copy
    Link produces. Accepting only the first told three real users that their own
    Salesforce link was "not from our configured org" — while Grant located the
    very same Campaign by name seconds later.

    The alias is DERIVED from the configured host by exact suffix substitution, so
    this admits exactly one additional hostname, computed from local configuration.
    It is not a pattern and cannot be widened by anything in a pasted link.
    """
    host = (urlparse(_write_my_domain()).hostname or "").lower()
    if not host:
        return frozenset()
    hosts = {host}
    suffix = ".my.salesforce.com"
    if host.endswith(suffix):
        hosts.add(f"{host[: -len(suffix)]}.lightning.force.com")
    return frozenset(hosts)


def validate_record_id(record_id: str, expected_sobject: str) -> str:
    """Validate a 15/18-character Salesforce ID and its object prefix."""
    clean = record_id.strip()
    expected_prefix = _ID_PREFIXES.get(expected_sobject)
    if expected_prefix is None:
        raise ValueError(f"unsupported Salesforce object '{expected_sobject}'")
    if (
        len(clean) not in (15, 18)
        or not clean.isalnum()
        or not clean.startswith(expected_prefix)
    ):
        raise ValueError(f"not a valid {expected_sobject} Salesforce ID")
    return clean


def parse_record_link(link: str, allowed_sobjects: set[str]) -> tuple[str, str]:
    """Validate hostname, Lightning object path, and Salesforce record prefix."""
    parsed = urlparse(link.strip())
    if not parsed.hostname or parsed.hostname.lower() not in _configured_hosts():
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

    reconciliation_delays: tuple[float, ...] = (0.0, 0.2, 0.8)

    def _auth(self, force: bool = False) -> tuple[str, str]:
        """Authenticate with the dedicated writer Connected App."""
        now = time.time()
        domain = _write_my_domain().rstrip("/")
        client_id = _write_client_id()
        credential_scope = f"{domain}|{client_id}"
        if (
            not force
            and _TOKEN_CACHE.token
            and _TOKEN_CACHE.expires_at > now
            and _TOKEN_CACHE.credential_scope == credential_scope
        ):
            return _TOKEN_CACHE.token, _TOKEN_CACHE.instance_url
        response = requests.post(
            f"{domain}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": _write_client_secret(),
            },
            timeout=20,
        )
        response.raise_for_status()
        body: dict[str, Any] = (
            response.json()
        )  # Salesforce OAuth JSON is runtime-shaped
        _TOKEN_CACHE.token = str(body["access_token"])
        _TOKEN_CACHE.instance_url = str(body.get("instance_url") or domain).rstrip("/")
        _TOKEN_CACHE.expires_at = now + 25 * 60
        _TOKEN_CACHE.credential_scope = credential_scope
        return _TOKEN_CACHE.token, _TOKEN_CACHE.instance_url

    def verify_write_scope(self) -> SalesforceOrganizationIdentity:
        """Require exact configured org identity and sandbox status before a write."""
        token, instance = self._auth()
        configured = _write_my_domain().rstrip("/")
        configured_parsed = urlparse(configured)
        instance_parsed = urlparse(instance)
        if configured_parsed.scheme != "https" or instance_parsed.scheme != "https":
            raise PermissionError("Salesforce writer endpoints must use HTTPS")
        configured_host = (configured_parsed.hostname or "").lower()
        instance_host = (instance_parsed.hostname or "").lower()
        if not configured_host or configured_host != instance_host:
            raise PermissionError(
                "Salesforce OAuth instance does not match the configured writer host"
            )
        expected_org_id = os.environ.get("SALESFORCE_WRITE_ORG_ID", "").strip()
        if not expected_org_id:
            raise PermissionError("SALESFORCE_WRITE_ORG_ID is not configured")
        validate_record_id(expected_org_id, "Organization")
        expected_flag = os.environ.get("SALESFORCE_WRITE_EXPECT_SANDBOX", "").strip()
        if expected_flag not in {"0", "1"}:
            raise PermissionError(
                "SALESFORCE_WRITE_EXPECT_SANDBOX must be explicitly 0 or 1"
            )
        expected_sandbox = expected_flag == "1"
        response = requests.get(
            f"{instance}/services/data/{API_VERSION}/query",
            params={
                "q": "SELECT Id,Name,IsSandbox,InstanceName FROM Organization LIMIT 2"
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        response.raise_for_status()
        records = response.json().get("records") or []
        if len(records) != 1:
            raise PermissionError("Salesforce Organization identity was not unique")
        record = records[0]
        actual_org_id = str(record.get("Id") or "")
        actual_sandbox = bool(record.get("IsSandbox"))
        if actual_org_id != expected_org_id:
            raise PermissionError(
                "Salesforce Organization ID does not match the configured allowlist"
            )
        if actual_sandbox != expected_sandbox:
            raise PermissionError(
                "Salesforce sandbox status does not match the configured expectation"
            )
        return SalesforceOrganizationIdentity(
            organization_id=actual_org_id,
            name=str(record.get("Name") or ""),
            is_sandbox=actual_sandbox,
            instance_name=str(record.get("InstanceName") or ""),
            instance_url=instance,
        )

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """Issue a read request with writer credentials for pre-create validation."""
        token, instance = self._auth()
        response = requests.get(
            f"{instance}/services/data/{API_VERSION}/{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]  # third-party JSON

    def _query_all(self, soql: str, cap: int = 5_000) -> list[dict[str, Any]]:
        """Read every bounded SOQL page and reject malformed pagination paths."""
        body = self._get("query", {"q": soql})
        records = list(body.get("records") or [])
        prefix = f"/services/data/{API_VERSION}/"
        while not bool(body.get("done", True)):
            next_path = str(body.get("nextRecordsUrl") or "")
            if not next_path.startswith(prefix):
                raise ValueError("Salesforce pagination path was malformed")
            body = self._get(next_path[len(prefix) :])
            records.extend(body.get("records") or [])
            if len(records) > cap:
                raise ValueError("Salesforce organization resolution exceeded its cap")
        return records

    def _create_one(self, sobject: str, payload: dict[str, object]) -> CreateResult:
        """Create one record on the explicit object allowlist."""
        if sobject not in _ALLOWED_CREATE_OBJECTS:
            raise ValueError(f"Salesforce create forbidden for {sobject}")
        self.verify_write_scope()
        token, instance = self._auth()
        response = requests.post(
            f"{instance}/services/data/{API_VERSION}/sobjects/{sobject}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if response.status_code not in (200, 201):
            return CreateResult(
                False, error=f"HTTP {response.status_code}: {response.text[:200]}"
            )
        body: dict[str, Any] = (
            response.json()
        )  # Salesforce create JSON is runtime-shaped
        return CreateResult(bool(body.get("success", True)), str(body.get("id") or ""))

    def _create_many(
        self, sobject: str, payloads: list[dict[str, object]]
    ) -> list[CreateResult]:
        """Create up to 200 records with per-record results and allOrNone disabled."""
        if sobject not in _ALLOWED_CREATE_OBJECTS:
            raise ValueError(f"Salesforce create forbidden for {sobject}")
        if len(payloads) > MAX_ACTION_ORGANIZATIONS:
            raise ValueError("Salesforce collection exceeds 200 records")
        if not payloads:
            return []
        self.verify_write_scope()
        token, instance = self._auth()
        records = [{"attributes": {"type": sobject}, **payload} for payload in payloads]
        response = requests.post(
            f"{instance}/services/data/{API_VERSION}/composite/sobjects",
            params={"allOrNone": "false"},
            json={"records": records},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
        body: list[dict[str, Any]] = response.json()  # third-party collection result
        results: list[CreateResult] = []
        for item in body:
            errors = item.get("errors") or []
            error = "; ".join(str(err.get("message") or err) for err in errors)
            results.append(
                CreateResult(
                    bool(item.get("success")),
                    str(item.get("id") or ""),
                    error,
                )
            )
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
                    str(item.get("value"))
                    for item in field.get("picklistValues") or []
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
        return [
            SalesforceRecordRef(
                "User",
                str(record["Id"]),
                str(record.get("Name") or ""),
                self.lightning_link("User", str(record["Id"])),
            )
            for record in records
        ]

    def search_campaigns(self, name: str) -> list[SalesforceRecordRef]:
        """Return exact/contains Campaign candidates for human selection."""
        literal = _soql_literal(name.strip())
        soql = (
            "SELECT Id,Name,Status,Type,IsActive,Owner.Name FROM Campaign "
            f"WHERE Name LIKE '%{literal}%' ORDER BY LastModifiedDate DESC LIMIT 20"
        )
        records = self._get("query", {"q": soql}).get("records") or []
        return [
            SalesforceRecordRef(
                "Campaign",
                str(record["Id"]),
                str(record.get("Name") or ""),
                self.lightning_link("Campaign", str(record["Id"])),
            )
            for record in records
        ]

    def get_record(self, sobject: str, record_id: str) -> SalesforceRecordRef:
        """Read back a Campaign, Lead, or Contact before showing confirmation."""
        validate_record_id(record_id, sobject)
        fields = {
            "Campaign": "Id,Name",
            "Lead": "Id,Name,Company,State",
            "Contact": "Id,Name,Account.Id,Account.Name,Account.BillingState",
            "Account": "Id,Name,BillingState",
        }[sobject]
        body = self._get(f"sobjects/{sobject}/{record_id}", {"fields": fields})
        account = body.get("Account") or {}
        return SalesforceRecordRef(
            sobject=sobject,
            record_id=record_id,
            name=str(body.get("Name") or ""),
            link=self.lightning_link(sobject, record_id),
            company=str(
                body.get("Company") or account.get("Name") or body.get("Name") or ""
            ),
            state=str(
                body.get("State")
                or account.get("BillingState")
                or body.get("BillingState")
                or ""
            ),
            account_id=str(
                account.get("Id") or (record_id if sobject == "Account" else "")
            ),
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
            f" AND Account.BillingState='{_soql_literal(state.upper())}'"
            if state
            else ""
        )
        contact_soql = (
            "SELECT Id,Name,Account.Id,Account.Name,Account.BillingState FROM Contact "
            f"WHERE Account.Name='{literal}'{contact_state} LIMIT 20"
        )
        records: list[tuple[str, dict[str, Any]]] = []
        records.extend(
            ("Lead", item)
            for item in self._get("query", {"q": lead_soql}).get("records") or []
        )
        records.extend(
            ("Contact", item)
            for item in self._get("query", {"q": contact_soql}).get("records") or []
        )
        refs: list[SalesforceRecordRef] = []
        for sobject, record in records:
            account = record.get("Account") or {}
            record_id = str(record["Id"])
            refs.append(
                SalesforceRecordRef(
                    sobject,
                    record_id,
                    str(record.get("Name") or ""),
                    self.lightning_link(sobject, record_id),
                    company=str(record.get("Company") or account.get("Name") or ""),
                    state=str(record.get("State") or account.get("BillingState") or ""),
                    account_id=str(account.get("Id") or ""),
                )
            )
        return refs

    def find_accounts(self, entity_name: str, state: str) -> list[SalesforceRecordRef]:
        """Find exact Accounts so organization-only Lead creation cannot duplicate one."""
        if not state.strip():
            return []
        literal = _soql_literal(entity_name.strip())
        state_literal = _soql_literal(state.strip().upper())
        soql = (
            "SELECT Id,Name,BillingState FROM Account "
            f"WHERE Name='{literal}' AND BillingState='{state_literal}' LIMIT 20"
        )
        records = self._get("query", {"q": soql}).get("records") or []
        return [
            SalesforceRecordRef(
                "Account",
                str(record["Id"]),
                str(record.get("Name") or ""),
                self.lightning_link("Account", str(record["Id"])),
                company=str(record.get("Name") or ""),
                state=str(record.get("BillingState") or ""),
                account_id=str(record["Id"]),
            )
            for record in records
        ]

    def resolve_organizations(
        self, organizations: list[tuple[str, str]]
    ) -> dict[str, tuple[list[SalesforceRecordRef], list[SalesforceRecordRef]]]:
        """Resolve exact Leads/Contacts/Accounts in bounded, paginated SOQL chunks."""
        requested = {
            db.canonical_entity_key(name, state): (name.strip(), state.strip().upper())
            for name, state in organizations
            if name.strip() and state.strip()
        }
        resolved: dict[
            str, tuple[list[SalesforceRecordRef], list[SalesforceRecordRef]]
        ] = {key: ([], []) for key in requested}
        values = list(requested.values())
        for offset in range(0, len(values), 20):
            chunk = values[offset : offset + 20]
            lead_where = " OR ".join(
                f"(Company='{_soql_literal(name)}' AND State='{_soql_literal(state)}')"
                for name, state in chunk
            )
            contact_where = " OR ".join(
                f"(Account.Name='{_soql_literal(name)}' "
                f"AND Account.BillingState='{_soql_literal(state)}')"
                for name, state in chunk
            )
            account_where = " OR ".join(
                f"(Name='{_soql_literal(name)}' "
                f"AND BillingState='{_soql_literal(state)}')"
                for name, state in chunk
            )
            lead_rows = self._query_all(
                f"SELECT Id,Name,Company,State FROM Lead WHERE {lead_where} LIMIT 2000"
            )
            contact_rows = self._query_all(
                "SELECT Id,Name,Account.Id,Account.Name,Account.BillingState "
                f"FROM Contact WHERE {contact_where} LIMIT 2000"
            )
            account_rows = self._query_all(
                "SELECT Id,Name,BillingState FROM Account "
                f"WHERE {account_where} LIMIT 2000"
            )
            for sobject, rows in (("Lead", lead_rows), ("Contact", contact_rows)):
                for record in rows:
                    account = record.get("Account") or {}
                    company = str(record.get("Company") or account.get("Name") or "")
                    state = str(
                        record.get("State") or account.get("BillingState") or ""
                    )
                    key = db.canonical_entity_key(company, state)
                    if key not in resolved:
                        continue
                    record_id = str(record["Id"])
                    resolved[key][0].append(
                        SalesforceRecordRef(
                            sobject,
                            record_id,
                            str(record.get("Name") or ""),
                            self.lightning_link(sobject, record_id),
                            company=company,
                            state=state,
                            account_id=str(account.get("Id") or ""),
                        )
                    )
            for record in account_rows:
                company = str(record.get("Name") or "")
                state = str(record.get("BillingState") or "")
                key = db.canonical_entity_key(company, state)
                if key not in resolved:
                    continue
                record_id = str(record["Id"])
                resolved[key][1].append(
                    SalesforceRecordRef(
                        "Account",
                        record_id,
                        company,
                        self.lightning_link("Account", record_id),
                        company=company,
                        state=state,
                        account_id=record_id,
                    )
                )
        return resolved

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
            raise ValueError(
                f"Campaign status '{MEMBER_STATUS}' incorrectly marks responded"
            )
        return True

    def existing_members(self, campaign_id: str, record_ids: list[str]) -> set[str]:
        """Return Lead/Contact IDs already present in the selected Campaign."""
        return set(self.member_records(campaign_id, record_ids))

    def member_records(
        self, campaign_id: str, record_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Return member target ID mapped to CampaignMember ID and current status."""
        if not record_ids:
            return {}
        quoted = ",".join(f"'{_soql_literal(item)}'" for item in record_ids)
        soql = (
            "SELECT Id,LeadId,ContactId,Status FROM CampaignMember "
            f"WHERE CampaignId='{campaign_id}' AND (LeadId IN ({quoted}) "
            f"OR ContactId IN ({quoted}))"
        )
        records = self._get("query", {"q": soql}).get("records") or []
        return {
            str(record.get("LeadId") or record.get("ContactId")): (
                str(record.get("Id") or ""),
                str(record.get("Status") or ""),
            )
            for record in records
        }

    def create_campaign(self, payload: dict[str, object]) -> CreateResult:
        """Create one Campaign."""
        return self._create_one("Campaign", payload)

    def create_member_status(self, campaign_id: str) -> CreateResult:
        """Create the disclosed non-response status without changing existing defaults."""
        return self._create_one(
            "CampaignMemberStatus",
            {
                "CampaignId": campaign_id,
                "Label": MEMBER_STATUS,
                "HasResponded": False,
            },
        )

    def create_leads(self, payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Create approved Lead records through the collection endpoint."""
        return self._create_many("Lead", payloads)

    def create_members(self, payloads: list[dict[str, object]]) -> list[CreateResult]:
        """Create approved Campaign Members with per-record results."""
        return self._create_many("CampaignMember", payloads)

    def create_lead(self, payload: dict[str, object]) -> CreateResult:
        """Create one person Lead through the single-record allowlisted path."""
        return self._create_one("Lead", payload)

    def fill_lead_blanks(
        self, record_id: str, proposed: dict[str, object]
    ) -> CreateResult:
        """Fill EMPTY fields on an existing Lead. Never overwrite, never clear.

        WHY THIS EXISTS AT ALL. Grant is create-only by design, and that guarantee is
        load-bearing — it is why "delete that campaign" is structurally impossible
        rather than merely refused. But 13 of 14 leads in a real campaign matched
        records that ALREADY existed in Salesforce, one of them imported in 2019 with
        no title, no mobile and no notes. A rep opening it saw an empty record and had
        to research an organization Grant had already researched.

        THE SHAPE IS THE SAFETY, not a policy check. The record is READ first, and a
        field is sent only when Salesforce currently holds nothing there AND Grant has
        a real value. So this can add information and cannot remove or contradict any,
        whatever the caller passes. The allowlist excludes identity and ownership.

        The returned `error` field carries a plain description of what was filled or
        why nothing was — it is the caller's material for telling a rep the truth.
        """
        validate_record_id(record_id, "Lead")
        self.verify_write_scope()
        token, instance = self._auth()
        headers = {"Authorization": f"Bearer {token}"}
        base = f"{instance}/services/data/{API_VERSION}/sobjects/Lead/{record_id}"

        wanted = {
            field: value
            for field, value in proposed.items()
            if field in _ALLOWED_LEAD_FILL_FIELDS and str(value or "").strip()
        }
        if not wanted:
            return CreateResult(True, record_id, error="nothing to fill")
        current = requests.get(
            base,
            params={"fields": ",".join(sorted(wanted))},
            headers=headers,
            timeout=20,
        )
        if current.status_code == 404:
            # NOT IN THIS ORG — a distinct, expected outcome rather than a failure.
            # `crm_action_items.salesforce_id` holds ids from BOTH the production org
            # and the monarchdev sandbox, with nothing recording which is which, so a
            # bulk fill legitimately meets ids that do not exist here. The pre-read is
            # what makes that safe: nothing is ever patched. Reporting it as
            # "HTTP 404" made a routine skip read like a broken integration.
            return CreateResult(False, record_id, error="not in this org")
        if current.status_code != 200:
            return CreateResult(
                False, error=f"HTTP {current.status_code}: {current.text[:200]}"
            )
        existing: dict[str, Any] = current.json()
        blanks = {
            field: value
            for field, value in wanted.items()
            if not str(existing.get(field) or "").strip()
        }
        if not blanks:
            return CreateResult(
                True, record_id, error="every field already had a value"
            )
        response = requests.patch(base, json=blanks, headers=headers, timeout=20)
        if response.status_code not in (200, 204):
            return CreateResult(
                False, error=f"HTTP {response.status_code}: {response.text[:200]}"
            )
        return CreateResult(
            True, record_id, error=f"filled {', '.join(sorted(blanks))}"
        )

    def create_note(self, payload: dict[str, object]) -> CreateResult:
        """Create one legacy Note attached to its ParentId (a Lead)."""
        return self._create_one("Note", payload)

    def create_content_note(
        self, parent_id: str, title: str, body_html: str
    ) -> CreateResult:
        """Create a Lightning 'Enhanced Note' (ContentNote) linked to a record.

        This is the note that appears in Lightning's *Notes* related list (the
        legacy Note object shows only under 'Notes & Attachments'). Two creates:
        the ContentNote (Content is base64 HTML), then a ContentDocumentLink that
        attaches it to the Lead. Returns the ContentNote CreateResult; a failed
        link is reported in the error so the caller can surface it honestly."""
        import base64

        content = base64.b64encode(body_html.encode("utf-8")).decode("ascii")
        note = self._create_one("ContentNote", {"Title": title, "Content": content})
        if not note.success or not note.record_id:
            return note
        # A ContentNote is stored as a ContentDocument (FileType SNOTE) whose own Id
        # IS its ContentDocumentId — so the document id is note.record_id directly,
        # with no lookup (and SOQL on ContentNote is rejected in some orgs anyway).
        link = self._create_one(
            "ContentDocumentLink",
            {
                "ContentDocumentId": note.record_id,
                "LinkedEntityId": parent_id,
                "ShareType": "V",
                "Visibility": "AllUsers",
            },
        )
        if not link.success:
            return CreateResult(
                False,
                note.record_id,
                error=f"note created but not linked: {link.error}",
            )
        return note

    def lead_record_type_id(self, developer_name: str) -> str:
        """Resolve one active Lead RecordType id by DeveloperName, or '' if absent.

        Cached per gateway instance; failing closed to '' lets the caller omit
        RecordTypeId and inherit the org default rather than send a bad id."""
        if developer_name in _RECORD_TYPE_CACHE:
            return _RECORD_TYPE_CACHE[developer_name]
        soql = (
            "SELECT Id FROM RecordType WHERE SobjectType='Lead' "
            f"AND DeveloperName='{_soql_literal(developer_name)}' "
            "AND IsActive=true LIMIT 1"
        )
        try:
            body = self._get("query", {"q": soql})
            records = body.get("records") or []
            record_id = str(records[0]["Id"]) if records else ""
        except (requests.RequestException, KeyError, IndexError):
            record_id = ""
        _RECORD_TYPE_CACHE[developer_name] = record_id
        return record_id
