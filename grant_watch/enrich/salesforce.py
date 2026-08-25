"""Strictly read-only Salesforce matching with explicit availability outcomes.

Account identity is established before Opportunity context is queried. Every CRM data
request goes through ``_readonly_get``; this module contains no data POST/PATCH/DELETE.
The OAuth token POST is authentication only. Campaign writes live in the separately
credentialed ``salesforce_campaigns`` module.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any  # Salesforce REST response JSON is runtime-shaped.
from urllib.parse import urlparse

import requests

from .. import state_codes

API_VERSION = os.environ.get("SALESFORCE_API_VERSION", "v60.0")
Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop


@dataclass
class _TokenCache:
    """In-process OAuth token cache; values never leave this module or logs."""

    access_token: str = ""
    instance_url: str = ""
    expires_at: float = 0.0
    credential_scope: str = ""


_TOKEN_CACHE = _TokenCache()


class SFResultStatus(str, Enum):
    """Honest outcome of a Salesforce lookup."""

    FOUND = "found"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SFMatch:
    """One Salesforce record reference with evidence-backed match confidence."""

    sobject: str
    record_id: str
    name: str
    company: str
    owner: str
    link: str
    confidence: str
    state: str = ""
    website: str = ""
    account_id: str = ""
    stage: str = ""
    is_closed: bool | None = None
    owner_id: str = ""
    owner_email: str = ""


@dataclass
class SFResult:
    """Typed lookup result; ``no_match`` is distinct from any outage."""

    status: SFResultStatus = SFResultStatus.NO_MATCH
    matches: list[SFMatch] = field(default_factory=list)
    error: str = ""
    checked_at: float = field(default_factory=time.time)
    attempted_terms: tuple[str, ...] = ()
    connected_host: str = ""

    @property
    def matched(self) -> bool:
        """Compatibility view used by Slack rendering."""
        return bool(self.matches)


_GENERIC_WORDS = {
    "school",
    "schools",
    "district",
    "districts",
    "unified",
    "consolidated",
    "public",
    "city",
    "town",
    "county",
    "of",
    "the",
    "inc",
    "llc",
    "corp",
    "corporation",
    "company",
    "co",
    "department",
    "dept",
    "hospital",
    "hospitals",
    "health",
    "system",
    "systems",
    "medical",
    "center",
    "authority",
    "board",
    "education",
    "isd",
    "usd",
}
_SOSL_RESERVED_RE = re.compile(r"[?&|!{}\[\]()^~*:\\\"'+\-.,]")


class SalesforceConfigurationError(ValueError):
    """Report absent read-only Salesforce credentials without leaking secrets."""


def _auth(force: bool = False) -> tuple[str, str]:
    """Return a cached reader token and instance URL, refreshing when requested."""
    now = time.time()
    required_reader_keys = (
        "SALESFORCE_MY_DOMAIN_URL",
        "SALESFORCE_CLIENT_ID",
        "SALESFORCE_CLIENT_SECRET",
    )
    missing_reader_keys = [
        key for key in required_reader_keys if not os.environ.get(key, "").strip()
    ]
    if missing_reader_keys:
        raise SalesforceConfigurationError(
            "Salesforce reader is not configured; missing "
            + ", ".join(missing_reader_keys)
        )
    domain = os.environ["SALESFORCE_MY_DOMAIN_URL"].rstrip("/")
    client_id = os.environ["SALESFORCE_CLIENT_ID"]
    credential_scope = f"{domain}|{client_id}"
    if (
        not force
        and _TOKEN_CACHE.access_token
        and _TOKEN_CACHE.expires_at > now
        and _TOKEN_CACHE.credential_scope == credential_scope
    ):
        return _TOKEN_CACHE.access_token, _TOKEN_CACHE.instance_url
    response = requests.post(
        f"{domain}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": os.environ["SALESFORCE_CLIENT_SECRET"],
        },
        timeout=20,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()  # third-party OAuth JSON is runtime-shaped
    _TOKEN_CACHE.access_token = str(body["access_token"])
    _TOKEN_CACHE.instance_url = str(body.get("instance_url") or domain).rstrip("/")
    _TOKEN_CACHE.expires_at = now + 25 * 60
    _TOKEN_CACHE.credential_scope = credential_scope
    return _TOKEN_CACHE.access_token, _TOKEN_CACHE.instance_url


def _readonly_get(
    path: str, params: dict[str, str], token: str, instance_url: str
) -> dict[str, Any]:
    """Issue one GET-only CRM request, retrying ONCE on a rejected session.

    WHY THE RETRY IS NOT OPTIONAL. Salesforce reports no token lifetime for the
    client_credentials flow -- the token response carries `issued_at` but no
    `expires_in` -- so `_auth` can only GUESS one, and it guesses 25 minutes. The
    real session length lives in the connected app's policy, where an admin can
    shorten it without anything here noticing. When the true lifetime is shorter
    than the guess, EVERY read fails from the moment the token actually dies until
    the guessed clock rolls over, and the caller sees only an exception class name.

    Recovering from the rejection is the only correct design when the server will
    not tell you the expiry. One retry, then an honest failure -- never a loop.
    """

    def fetch(bearer: str, host: str) -> requests.Response:
        """Perform the GET itself, so the retry cannot drift from the first call."""
        return requests.get(
            f"{host}/services/data/{API_VERSION}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=20,
        )

    response = fetch(token, instance_url)
    if response.status_code == 401:
        # The guessed expiry was wrong. Re-authenticate and try exactly once more.
        token, instance_url = _auth(force=True)
        response = fetch(token, instance_url)
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]  # requests JSON is untyped


def readonly_soql(query: str) -> tuple[list[dict[str, Any]], str]:
    """Run a paginated SOQL query through the reader-only GET transport.

    This intentionally exposes reads only; callers cannot select an HTTP method.
    The returned host is useful for constructing evidence links without trusting
    an environment URL that Salesforce may redirect during OAuth.
    """
    token, instance_url = _auth()
    body = _readonly_get("query", {"q": query}, token, instance_url)
    records: list[dict[str, Any]] = list(body.get("records") or [])
    while not bool(body.get("done", True)):
        next_url = str(body.get("nextRecordsUrl") or "")
        prefix = f"/services/data/{API_VERSION}/"
        if not next_url.startswith(prefix):
            raise RuntimeError("Salesforce returned an invalid pagination URL")
        body = _readonly_get(next_url.removeprefix(prefix), {}, token, instance_url)
        records.extend(body.get("records") or [])
    return records, instance_url


def _identity(value: str) -> tuple[frozenset[str], frozenset[str]]:
    """Split an organization name into its WORDS and its RECORD NUMBERS.

    These are two different kinds of evidence and collapsing them into one token set
    got both halves wrong. `"428".isdigit()` is True but `"#428".isdigit()` is False,
    so the same district written "District #428" and "District 428" produced token
    sets that could never be equal -- and `_confidence` only reaches "high" on
    equality. But simply STRIPPING the number is worse: for "Baboquivari Unified
    School District #40" the distinctive words are just {"baboquivari"}, so dropping
    the number takes the name under the two-token threshold and NO name can ever be
    confident again. That failure is a quiet refusal, indistinguishable from correct
    caution. Measured 2026-08-25: it would have hit 14 of the 34 "#" leads.

    For "INDEPENDENT SCHOOL DISTRICT #492" the number IS the identity -- it is the
    only thing separating that district from every other one with the same name.
    So numbers are kept, compared separately, and matched by INTERSECTION rather
    than equality: a real CRM record may carry an extra code (production holds
    "BABOQUIVARI ... #40 (4412)") and one side having a number the other lacks must
    not break an otherwise exact match.
    """
    words: set[str] = set()
    numbers: set[str] = set()
    for raw in distinctive_term(value).split():
        bare = re.sub(r"\W+", "", raw.lower())
        if not bare:
            continue
        if bare.isdigit():
            numbers.add(bare.lstrip("0") or "0")
        else:
            words.add(bare)
    return frozenset(words), frozenset(numbers)


def _is_record_number(word: str) -> bool:
    """True when a word is a bare record number, however it is punctuated.

    `search_terms` uses this to build its most TOLERANT variant. `"#428".isdigit()`
    is False, so for a "#NNN" name that variant came out identical to the previous
    one, was de-duplicated away, and the broad search-by-name-alone was never issued.
    """
    return re.sub(r"\W+", "", word).isdigit()


def distinctive_term(entity: str) -> str:
    """Remove SOSL punctuation and generic organization words from an entity."""
    cleaned = _SOSL_RESERVED_RE.sub(" ", entity)
    words = [
        word
        for word in cleaned.split()
        if len(word) > 1 and word.lower() not in _GENERIC_WORDS
    ]
    return " ".join(words) or cleaned.strip()


def search_terms(entity: str) -> tuple[str, ...]:
    """Return bounded SOSL variants from precise to tolerant for source/CRM drift."""
    cleaned = " ".join(_SOSL_RESERVED_RE.sub(" ", entity).split())
    distinctive = distinctive_term(entity)
    without_number = " ".join(
        word for word in distinctive.split() if not _is_record_number(word)
    )
    return tuple(
        dict.fromkeys(term for term in (cleaned, distinctive, without_number) if term)
    )


def _tokens(value: str) -> frozenset[str]:
    """Every normalized identity token, words and record numbers together."""
    words, numbers = _identity(value)
    return words | numbers


def _domain(value: str) -> str:
    """Normalize a website/domain to a lower-case hostname."""
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower().removeprefix("www.")


def _digits(value: str) -> str:
    """Return phone digits only for deterministic suffix comparisons."""
    return re.sub(r"\D", "", value)


def _confidence(
    entity: str,
    candidate: str,
    requested_state: str,
    candidate_state: str,
    requested_domain: str,
    candidate_domain: str,
    requested_phone: str,
    candidate_phone: str,
) -> str | None:
    """Classify a candidate without allowing one-word overlaps to be high confidence."""
    # Compare states by CODE, not by raw text. Salesforce holds the same state as
    # "IL" on one record and "Illinois" on another, and the raw comparison called
    # that a conflict -- which forces `_confidence` down the exact-token-equality
    # path and drops a genuine match. Unrecognized values fall back to the raw
    # upper-cased text, so a value neither side can parse still fails safe.
    requested_code = (
        state_codes.state_code_or_blank(requested_state) or requested_state.upper()
    )
    candidate_code = (
        state_codes.state_code_or_blank(candidate_state) or candidate_state.upper()
    )
    state_conflict = bool(
        requested_code and candidate_code and requested_code != candidate_code
    )
    wanted_words, wanted_numbers = _identity(entity)
    found_words, found_numbers = _identity(candidate)
    if not (wanted_words or wanted_numbers) or not (found_words or found_numbers):
        return None
    # A record number is IDENTITY, not a name word -- "#492" is the only thing
    # separating one "Independent School District" from another -- so one distinctive
    # word plus an AGREEING number is as strong as two matching words. Numbers are
    # compared by intersection so an extra code on the CRM side cannot break an
    # otherwise exact match.
    numbers_agree = bool(wanted_numbers & found_numbers)
    names_agree = bool(wanted_words) and wanted_words == found_words
    confident_name = names_agree and (len(wanted_words) >= 2 or numbers_agree)
    if state_conflict:
        # An exact name with conflicting geography is useful for a human to resolve,
        # but it can never be promoted to a confirmed organization match.
        return "possible" if confident_name else None
    req_domain = _domain(requested_domain)
    cand_domain = _domain(candidate_domain)
    if req_domain and cand_domain and req_domain == cand_domain:
        return "high"
    req_phone = _digits(requested_phone)
    cand_phone = _digits(candidate_phone)
    if (
        len(req_phone) >= 7
        and len(cand_phone) >= 7
        and req_phone[-7:] == cand_phone[-7:]
    ):
        return "high"
    if confident_name:
        return "high"
    if (wanted_words & found_words) or numbers_agree:
        return "possible"
    return None


def _link(instance_url: str, sobject: str, record_id: str) -> str:
    """Build a Lightning record URL from an ID returned by Salesforce."""
    return f"{instance_url}/lightning/r/{sobject}/{record_id}/view"


def _query_accounts(entity: str, token: str, instance_url: str) -> list[dict[str, Any]]:
    """Search Account using bounded name variants and deduplicate record IDs."""
    records: dict[str, dict[str, Any]] = {}
    for term in search_terms(entity):
        sosl = (
            f"FIND {{{term}}} IN ALL FIELDS RETURNING "
            "Account(Id,Name,BillingState,BillingCity,Website,Phone,"
            "Owner.Id,Owner.Name,Owner.Email LIMIT 20)"
        )
        body = _readonly_get("search", {"q": sosl}, token, instance_url)
        for record in body.get("searchRecords") or []:
            records[str(record.get("Id") or "")] = record
    return [record for record_id, record in records.items() if record_id]


def _query_people(entity: str, token: str, instance_url: str) -> list[dict[str, Any]]:
    """Search Lead/Contact with the same bounded variants used for Accounts."""
    records: dict[str, dict[str, Any]] = {}
    for term in search_terms(entity):
        sosl = (
            f"FIND {{{term}}} IN ALL FIELDS RETURNING "
            "Lead(Id,Name,Company,State,Website,Phone,"
            "Owner.Id,Owner.Name,Owner.Email LIMIT 20), "
            "Contact(Id,Name,MailingState,Phone,Owner.Id,Owner.Name,Owner.Email,"
            "Account.Id,Account.Name LIMIT 20)"
        )
        body = _readonly_get("search", {"q": sosl}, token, instance_url)
        for record in body.get("searchRecords") or []:
            records[str(record.get("Id") or "")] = record
    return [record for record_id, record in records.items() if record_id]


def _query_opportunities(
    account_id: str, token: str, instance_url: str
) -> list[dict[str, Any]]:
    """Return open Opportunities related to exactly one confirmed Account."""
    safe_id = re.sub(r"[^A-Za-z0-9]", "", account_id)
    soql = (
        "SELECT Id,Name,Owner.Id,Owner.Name,Owner.Email,"
        "StageName,IsClosed,AccountId,CloseDate "
        f"FROM Opportunity WHERE AccountId='{safe_id}' AND IsClosed=false "
        "ORDER BY CloseDate ASC LIMIT 20"
    )
    body = _readonly_get("query", {"q": soql}, token, instance_url)
    return list(body.get("records") or [])


def lookup(
    entity: str,
    domain: str = "",
    phone: str = "",
    state: str = "",
    on_progress: Progress | None = None,
) -> SFResult:
    """Find Account/people context and only Account-bound open Opportunities.

    A successful, complete Account query is required for ``NO_MATCH``. Any failed
    Account request is ``UNAVAILABLE``; later failures produce ``PARTIAL``.
    """
    say = on_progress or _NOOP
    say("Checking Salesforce")
    attempted_terms = search_terms(entity)
    connected_host = (
        urlparse(os.environ.get("SALESFORCE_MY_DOMAIN_URL", "")).hostname or ""
    )
    try:
        token, instance_url = _auth()
        account_records = _query_accounts(entity, token, instance_url)
    except SalesforceConfigurationError as exc:
        return SFResult(
            status=SFResultStatus.UNAVAILABLE,
            error=str(exc),
            attempted_terms=attempted_terms,
            connected_host=connected_host,
        )
    except (requests.RequestException, KeyError, ValueError) as exc:
        return SFResult(
            status=SFResultStatus.UNAVAILABLE,
            error=f"Salesforce account lookup failed ({type(exc).__name__})",
            attempted_terms=attempted_terms,
            connected_host=connected_host,
        )

    matches: list[SFMatch] = []
    high_accounts: list[SFMatch] = []
    for record in account_records:
        candidate_name = str(record.get("Name") or "")
        candidate_state = str(record.get("BillingState") or "")
        confidence = _confidence(
            entity,
            candidate_name,
            state,
            candidate_state,
            domain,
            str(record.get("Website") or ""),
            phone,
            str(record.get("Phone") or ""),
        )
        if confidence is None:
            continue
        owner_record = record.get("Owner") or {}
        owner = str(owner_record.get("Name") or "")
        match = SFMatch(
            sobject="Account",
            record_id=str(record["Id"]),
            name=candidate_name,
            company="",
            owner=owner,
            link=_link(instance_url, "Account", str(record["Id"])),
            confidence=confidence,
            state=candidate_state,
            website=str(record.get("Website") or ""),
            owner_id=str(owner_record.get("Id") or ""),
            owner_email=str(owner_record.get("Email") or ""),
        )
        matches.append(match)
        if confidence == "high":
            high_accounts.append(match)

    partial = False
    try:
        people_records = _query_people(entity, token, instance_url)
    except requests.RequestException:
        people_records = []
        partial = True
    for record in people_records:
        sobject = str((record.get("attributes") or {}).get("type") or "")
        account = record.get("Account") or {}
        company = str(record.get("Company") or account.get("Name") or "")
        candidate_state = str(record.get("State") or record.get("MailingState") or "")
        confidence = _confidence(
            entity,
            company,
            state,
            candidate_state,
            domain,
            str(record.get("Website") or ""),
            phone,
            str(record.get("Phone") or ""),
        )
        if confidence is None:
            continue
        record_id = str(record.get("Id") or "")
        owner_record = record.get("Owner") or {}
        matches.append(
            SFMatch(
                sobject=sobject,
                record_id=record_id,
                name=str(record.get("Name") or ""),
                company=company,
                owner=str(owner_record.get("Name") or ""),
                link=_link(instance_url, sobject, record_id),
                confidence=confidence,
                state=candidate_state,
                website=str(record.get("Website") or ""),
                account_id=str(account.get("Id") or ""),
                owner_id=str(owner_record.get("Id") or ""),
                owner_email=str(owner_record.get("Email") or ""),
            )
        )

    if len(high_accounts) == 1:
        account = high_accounts[0]
        try:
            opportunities = _query_opportunities(account.record_id, token, instance_url)
        except requests.RequestException:
            opportunities = []
            partial = True
        for opportunity in opportunities:
            record_id = str(opportunity.get("Id") or "")
            owner_record = opportunity.get("Owner") or {}
            matches.append(
                SFMatch(
                    sobject="Opportunity",
                    record_id=record_id,
                    name=str(opportunity.get("Name") or ""),
                    company="",
                    owner=str(owner_record.get("Name") or ""),
                    link=_link(instance_url, "Opportunity", record_id),
                    confidence="high",
                    account_id=account.record_id,
                    stage=str(opportunity.get("StageName") or ""),
                    is_closed=bool(opportunity.get("IsClosed")),
                    owner_id=str(owner_record.get("Id") or ""),
                    owner_email=str(owner_record.get("Email") or ""),
                )
            )

    matches.sort(
        key=lambda item: (
            0 if item.confidence == "high" else 1,
            0 if item.sobject == "Account" else 1,
            item.name.lower(),
        )
    )
    high_people_count = sum(
        1
        for item in matches
        if item.confidence == "high" and item.sobject in {"Lead", "Contact"}
    )
    if partial:
        status = SFResultStatus.PARTIAL
    elif len(high_accounts) > 1:
        status = SFResultStatus.AMBIGUOUS
    elif len(high_accounts) == 1:
        # Multiple people under one confirmed Account are expected and do not make
        # the organization's identity ambiguous.
        status = SFResultStatus.FOUND
    elif high_people_count > 1 or (not high_people_count and matches):
        status = SFResultStatus.AMBIGUOUS
    elif matches:
        status = SFResultStatus.FOUND
    else:
        status = SFResultStatus.NO_MATCH
    if matches:
        say(f"Found {len(matches)} in Salesforce")
    return SFResult(
        status=status,
        matches=matches,
        attempted_terms=attempted_terms,
        connected_host=connected_host,
    )
