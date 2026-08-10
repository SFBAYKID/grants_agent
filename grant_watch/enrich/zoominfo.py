"""ZoomInfo contact data — the licensed-vendor rung of Grant's enrichment chain.

WHY THIS EXISTS. Grant's own discovery (``enrich/finder.py``) only trusts an email it
finds VERBATIM on the organization's official page. That gate is honest and strong, but
it can only reach what a public page actually publishes: on a real 10-organization batch
in Slack it produced exactly one usable email. ZoomInfo carries the rest.

WHY IT IS KEPT AT ARM'S LENGTH. ZoomInfo data is LICENSED VENDOR DATA, not page-verified
evidence. It can never be laundered into looking like finder's verbatim proof, so every
record this module returns carries ``provenance='zoominfo_vendor'`` and nothing here ever
writes a ``verified`` contact row. Rule 1 governs: a plausible-looking email that no one
checked is exactly the fabricated lead the Constitution forbids.

COST MODEL — the whole design rests on this asymmetry (`verified` live 2026-08-09):
  search  FREE. Returns names, titles, and BOOLEAN ``has*`` flags — you learn WHETHER a
          person has a mobile number without paying to see it. Only request limits apply.
  enrich  1 CREDIT per RETURNED record. A ``NO_MATCH`` or an error is NOT charged. Bounded
          to 25 records per call by the vendor.
So the honest flow is: search for free, show the rep exactly what a pull would cost, take
an explicit yes, then spend. Never spend to discover a dead end.

DO-NOT-CALL. Search returns ``directPhoneDoNotCall`` / ``mobilePhoneDoNotCall`` and enrich
can return an ``OPT_OUT`` match status. Roughly a third of the tech/ops contacts measured
in Grant's own lead set carry a DNC flag. These are propagated, never dropped — a caller
that discards them is creating legal exposure, not tidying a payload.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any  # vendor JSON is runtime-shaped; parsed into typed models below

import requests

API_BASE = "https://api.zoominfo.com/gtm"
TOKEN_URL = "https://okta-login.zoominfo.com/oauth2/default/v1/token"

# Cloudflare sits in front of BOTH the token endpoint and the API and returns a 1010
# "browser signature banned" 403 for the default python-urllib / python-requests
# User-Agent (`verified` 2026-08-09: identical request succeeded only once this header
# was set). Without this every call fails as an opaque 403 that looks like bad
# credentials, so this header is load-bearing, not cosmetic.
USER_AGENT = "MonarchConnected-GrantWatch/1.0 (+chase@monarchconnected.com)"

# Vendor-imposed ceiling on matchPersonInput; exceeding it is a 400, not a truncation.
MAX_ENRICH_BATCH = 25
# Vendor-imposed ceiling on page[size] for search.
MAX_SEARCH_PAGE_SIZE = 100

# Fields asked of the paid enrich endpoint. Kept explicit so the cost of a call is
# auditable from source and no one silently widens it.
#
# `directPhone` is DELIBERATELY ABSENT: this account's plan does not license it, and
# asking for it makes the WHOLE request a 400 — one disallowed field fails every
# record in the batch, not just that column (`verified` live 2026-08-09, the API says
# "OutputFields invalid or disallowed. Please contact your ZoomInfo Account Manager").
# Search still reports has_direct_phone, so a direct line can be seen to EXIST while
# being unavailable to buy; the preview must never promise one.
ENRICH_OUTPUT_FIELDS = (
    "id",
    "firstName",
    "lastName",
    "jobTitle",
    "email",
    "mobilePhone",
    "companyName",
    "directPhoneDoNotCall",
    "mobilePhoneDoNotCall",
    "contactAccuracyScore",
)

_TOKEN_MARGIN_S = 300.0  # refresh early; a token that expires mid-batch costs a retry

# Okta REFUSES a client_credentials grant that names no scope — the token endpoint
# answers 400, which reads exactly like a bad secret and sent one live test chasing
# the credential instead of the request. Only what this module actually calls is
# requested; the app also grants intent/news/scoops, which nothing here uses.
TOKEN_SCOPE = "api:data:contact api:data:company"


class ZoomInfoConfigurationError(ValueError):
    """Report absent ZoomInfo credentials without ever echoing a secret."""


class ZoomInfoUnavailable(RuntimeError):
    """ZoomInfo could not be reached or refused the request.

    Deliberately distinct from "no results": an outage must never be recorded as a
    not-found contact, exactly as finder.SourceUnreachable is distinct from a miss.
    """


@dataclass
class _TokenCache:
    """Process-local bearer token cache, scoped to the credential that minted it."""

    access_token: str = ""
    expires_at: float = 0.0
    credential_scope: str = ""


_TOKEN_CACHE = _TokenCache()


@dataclass(frozen=True)
class ZoomInfoContactMatch:
    """One FREE search hit: who exists, and which fields a paid pull would return.

    The ``has_*`` booleans are the product of the whole design — they let Grant quote an
    exact credit cost and let a rep decide, before a single credit is spent.
    """

    person_id: str
    first_name: str
    last_name: str
    job_title: str
    company_name: str
    has_email: bool
    has_direct_phone: bool
    has_mobile_phone: bool
    has_supplemental_email: bool
    direct_phone_do_not_call: bool
    mobile_phone_do_not_call: bool
    contact_accuracy_score: float | None = None
    last_updated: str = ""

    @property
    def display_name(self) -> str:
        """Full name for display, collapsing missing halves without stray spaces."""
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def do_not_call(self) -> bool:
        """True when EITHER stored number is flagged do-not-call.

        Deliberately pessimistic: a contact with one callable and one flagged number is
        treated as do-not-call, because the caller does not get to pick which line the
        flag applied to once the numbers are merged into a CRM record.
        """
        return self.direct_phone_do_not_call or self.mobile_phone_do_not_call


@dataclass(frozen=True)
class ZoomInfoContactDetail:
    """One PAID enrich result — actual reachable values, still vendor-sourced.

    ``match_status`` is the vendor's own verdict (FULL_MATCH / NO_MATCH / OPT_OUT / ...).
    Anything other than a full match must not be presented as a found contact.
    """

    person_id: str
    first_name: str
    last_name: str
    job_title: str
    company_name: str
    email: str
    direct_phone: str
    mobile_phone: str
    direct_phone_do_not_call: bool
    mobile_phone_do_not_call: bool
    match_status: str
    contact_accuracy_score: float | None = None

    @property
    def display_name(self) -> str:
        """Full name for display, collapsing missing halves without stray spaces."""
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def do_not_call(self) -> bool:
        """True when either stored number is flagged do-not-call (pessimistic)."""
        return self.direct_phone_do_not_call or self.mobile_phone_do_not_call

    @property
    def matched(self) -> bool:
        """True only on an unambiguous vendor full match."""
        return self.match_status.upper() == "FULL_MATCH"


@dataclass(frozen=True)
class CreditQuote:
    """What a paid pull WOULD cost, derived entirely from free search results.

    This is the object a human approves. ``billable`` counts only records the vendor
    would actually charge for, so the number a rep says yes to is the number spent.
    """

    matches: tuple[ZoomInfoContactMatch, ...] = field(default=())

    @property
    def billable(self) -> int:
        """Credits a pull of every match would consume (1 per returned record)."""
        return len(self.matches)

    @property
    def with_email(self) -> int:
        """How many of the quoted records carry an email address."""
        return sum(1 for match in self.matches if match.has_email)

    @property
    def with_phone(self) -> int:
        """How many carry a direct line or a mobile number."""
        return sum(
            1
            for match in self.matches
            if match.has_direct_phone or match.has_mobile_phone
        )

    @property
    def do_not_call(self) -> int:
        """How many are flagged do-not-call and must never be auto-dialed."""
        return sum(1 for match in self.matches if match.do_not_call)


def configured() -> bool:
    """True when both ZoomInfo credentials are present, without validating them."""
    return bool(
        os.environ.get("ZOOMINFO_CLIENT_ID", "").strip()
        and os.environ.get("ZOOMINFO_CLIENT_SECRET", "").strip()
    )


def _auth(force: bool = False) -> str:
    """Return a cached bearer token, minting a new one when absent or near expiry."""
    client_id = os.environ.get("ZOOMINFO_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ZOOMINFO_CLIENT_SECRET", "").strip()
    missing = [
        name
        for name, value in (
            ("ZOOMINFO_CLIENT_ID", client_id),
            ("ZOOMINFO_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise ZoomInfoConfigurationError(
            "ZoomInfo is not configured; missing " + ", ".join(missing)
        )
    now = time.time()
    if (
        not force
        and _TOKEN_CACHE.access_token
        and _TOKEN_CACHE.expires_at > now
        and _TOKEN_CACHE.credential_scope == client_id
    ):
        return _TOKEN_CACHE.access_token
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        response = requests.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials", "scope": TOKEN_SCOPE},
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            timeout=30,
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
    except requests.RequestException as exc:
        raise ZoomInfoUnavailable(
            f"ZoomInfo token request failed ({type(exc).__name__})"
        ) from exc
    token = str(body.get("access_token") or "")
    if not token:
        raise ZoomInfoUnavailable("ZoomInfo returned no access token")
    _TOKEN_CACHE.access_token = token
    _TOKEN_CACHE.expires_at = now + max(
        60.0, float(body.get("expires_in") or 3600) - _TOKEN_MARGIN_S
    )
    _TOKEN_CACHE.credential_scope = client_id
    return token


def _post(
    path: str, payload: dict[str, Any], *, timeout: float = 45.0
) -> dict[str, Any]:
    """POST one JSON:API request and return its runtime-shaped body."""
    token = _auth()
    try:
        response = requests.post(
            f"{API_BASE}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
    except requests.RequestException as exc:
        detail = ""
        response = getattr(exc, "response", None)
        if response is not None:
            # The vendor names the offending field here; without it a 400 is
            # indistinguishable from bad credentials.
            detail = f": {response.text[:300]}"
        raise ZoomInfoUnavailable(
            f"ZoomInfo request to {path} failed ({type(exc).__name__}){detail}"
        ) from exc
    return body


def _as_bool(value: object) -> bool:
    """Coerce a vendor JSON flag to bool without treating absence as False-positive."""
    return value is True


def _as_score(value: object) -> float | None:
    """Parse an accuracy score, returning None rather than guessing a default."""
    try:
        return float(value)  # type: ignore[arg-type]  # vendor sends str or number
    except (TypeError, ValueError):
        return None


def search_contacts(
    company_name: str,
    *,
    state: str = "",
    job_title: str = "",
    limit: int = 25,
) -> list[ZoomInfoContactMatch]:
    """Find WHO exists at an organization. FREE — consumes no credits.

    Returns typed matches carrying the ``has_*`` availability flags and do-not-call
    flags, which is everything needed to quote a paid pull's cost and let a human
    approve it. An empty list means the vendor genuinely has no one; an outage raises
    ZoomInfoUnavailable so a miss is never confused with a failure.
    """
    if not company_name.strip():
        raise ValueError("company_name is required for a ZoomInfo contact search")
    attributes: dict[str, Any] = {"companyName": company_name.strip()}
    if state.strip():
        attributes["state"] = state.strip().upper()
    if job_title.strip():
        attributes["jobTitle"] = job_title.strip()
    page_size = max(1, min(int(limit), MAX_SEARCH_PAGE_SIZE))
    body = _post(
        f"/data/v1/contacts/search?page%5Bsize%5D={page_size}",
        {"data": {"type": "ContactSearch", "attributes": attributes}},
    )
    matches: list[ZoomInfoContactMatch] = []
    for record in body.get("data") or []:
        attrs: dict[str, Any] = record.get("attributes") or {}
        matches.append(
            ZoomInfoContactMatch(
                person_id=str(record.get("id") or ""),
                first_name=str(attrs.get("firstName") or ""),
                last_name=str(attrs.get("lastName") or ""),
                job_title=str(attrs.get("jobTitle") or ""),
                company_name=str((attrs.get("company") or {}).get("name") or ""),
                has_email=_as_bool(attrs.get("hasEmail")),
                has_direct_phone=_as_bool(attrs.get("hasDirectPhone")),
                has_mobile_phone=_as_bool(attrs.get("hasMobilePhone")),
                has_supplemental_email=_as_bool(attrs.get("hasSupplementalEmail")),
                direct_phone_do_not_call=_as_bool(attrs.get("directPhoneDoNotCall")),
                mobile_phone_do_not_call=_as_bool(attrs.get("mobilePhoneDoNotCall")),
                contact_accuracy_score=_as_score(attrs.get("contactAccuracyScore")),
                last_updated=str(attrs.get("lastUpdatedDate") or ""),
            )
        )
    return matches


def quote(matches: list[ZoomInfoContactMatch]) -> CreditQuote:
    """Turn free search results into the exact cost a human is asked to approve."""
    return CreditQuote(matches=tuple(matches))


def enrich_contacts(person_ids: list[str]) -> list[ZoomInfoContactDetail]:
    """Retrieve actual emails and phone numbers. COSTS ONE CREDIT PER RETURNED RECORD.

    This function spends money and MUST NOT be called directly from a conversation
    path. Route it through ``enrich/zoominfo_credits.spend`` so the monthly budget,
    the durable ledger, and the human approval gate all apply.

    A ``NO_MATCH`` costs nothing, so unmatched rows are returned with their status
    intact rather than dropped — the caller needs them to reconcile what it paid for.

    `verified` 2026-08-10 against the live account, three times now (3 credits total).
    Two facts worth keeping, both learned by paying rather than by reading the docs:

    ``direct_phone`` comes back EMPTY even for a person whose search result says
    ``has_direct_phone`` — that column is not licensed on this plan, and asking for it
    by name 400s the whole batch. ``mobile_phone`` IS licensed and populates: a real
    pull returned ``(916) 505-3254`` for a Director of Facilities.

    So a direct line can be seen to EXIST while being unavailable to buy. Never
    promise a rep one.
    """
    ids = [pid.strip() for pid in person_ids if pid.strip()]
    if not ids:
        return []
    if len(ids) > MAX_ENRICH_BATCH:
        raise ValueError(
            f"ZoomInfo enrich accepts at most {MAX_ENRICH_BATCH} records per call; "
            f"got {len(ids)} — chunk before calling."
        )
    body = _post(
        "/data/v1/contacts/enrich",
        {
            "data": {
                "type": "ContactEnrich",
                "attributes": {
                    "matchPersonInput": [{"personId": int(pid)} for pid in ids],
                    "outputFields": list(ENRICH_OUTPUT_FIELDS),
                },
            }
        },
    )
    details: list[ZoomInfoContactDetail] = []
    for record in body.get("data") or []:
        attrs: dict[str, Any] = record.get("attributes") or {}
        meta: dict[str, Any] = record.get("meta") or {}
        details.append(
            ZoomInfoContactDetail(
                person_id=str(record.get("id") or ""),
                first_name=str(attrs.get("firstName") or ""),
                last_name=str(attrs.get("lastName") or ""),
                job_title=str(attrs.get("jobTitle") or ""),
                company_name=str(attrs.get("companyName") or ""),
                email=str(attrs.get("email") or ""),
                direct_phone=str(attrs.get("directPhone") or ""),
                mobile_phone=str(attrs.get("mobilePhone") or ""),
                direct_phone_do_not_call=_as_bool(attrs.get("directPhoneDoNotCall")),
                mobile_phone_do_not_call=_as_bool(attrs.get("mobilePhoneDoNotCall")),
                match_status=str(meta.get("matchStatus") or record.get("type") or ""),
                contact_accuracy_score=_as_score(attrs.get("contactAccuracyScore")),
            )
        )
    return details


def billable_records(details: list[ZoomInfoContactDetail]) -> int:
    """Count records the vendor actually charges for.

    Per the published contract a NO_MATCH and an error are free, so the ledger must
    record what was BILLED, not what was requested — otherwise a batch of misses
    silently burns a rep's monthly allowance on paper while costing nothing in fact.
    """
    return sum(1 for detail in details if detail.matched)
