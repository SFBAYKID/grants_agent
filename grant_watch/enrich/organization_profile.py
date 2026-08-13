"""Verbatim-verified organization profile enrichment for Salesforce records.

Given a Grant lead, this scrapes the organization's official website and extracts
org-level facts a rep needs on a CRM Lead — general email, main phone, mailing
street/city/state/zip, and the site URL — with the SAME anti-hallucination
discipline as finder.py: every value must appear verbatim on a page we actually
fetched, or it is dropped. It records nothing it could not read (honest
``unreachable``) and never invents an address, phone, or email.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlparse

from anthropic import Anthropic

from ..llm import anthropic_client_options
from . import evidence
from .finder import (
    MODEL,
    Progress,
    SourceUnreachable,
    _NOOP,
    _host,
    _looks_official,
    _scrape,
    _search,
)

# Contact-style pages most likely to carry an org's address, phone, and general
# mailbox. Tried in order until the address is verified or the budget is spent.
_CONTACT_PATHS = ("", "/contact", "/contact-us", "/about", "/about-us")
_MAX_PAGES = 4
# A general org mailbox is not tied to a named person; a personal alias is.
_GENERIC_LOCALPARTS = (
    "info",
    "office",
    "contact",
    "admin",
    "mail",
    "hello",
    "reception",
    "frontdesk",
    "main",
)


@dataclass
class OrgProfile:
    """Organization facts proven together on one fetched evidence page."""

    website: str = ""
    website_candidate: str = ""
    general_email: str = ""
    phone: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    source_url: str = ""
    status: str = "not_found"  # found | not_found | unreachable
    field_evidence: dict[str, evidence.EvidenceMatch] = field(default_factory=dict)


@dataclass(frozen=True)
class SiteCandidate:
    """One resolved site with evidence saying whether it is authoritative."""

    origin: str
    host: str
    evidence: evidence.EvidenceMatch
    authoritative: bool = False


def _general_email_on_page(page_text: str, email: str) -> bool:
    """A general org email must appear verbatim; unlike a person's, it needs no name.

    It must also read like a shared mailbox (info@/office@/…), so a stray
    personal address on the page is not mistaken for the organization's."""
    if evidence.exact_email(page_text, email, "", field="general_email") is None:
        return False
    localpart = email.split("@", 1)[0].lower()
    return localpart in _GENERIC_LOCALPARTS


def _origin(url: str) -> str:
    """Return the HTTP(S) origin for a URL, preserving its proven scheme."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"


def _resolve_site(conn: sqlite3.Connection, lead: sqlite3.Row) -> SiteCandidate | None:
    """Find a state-bound website candidate from verified contact evidence or search."""
    from .. import db

    nces_website = str(lead["nces_website"] or "").strip()
    nces_source = str(lead["nces_website_source_url"] or "").strip()
    if str(lead["nces_website_status"] or "") == "verified" and nces_website:
        origin = _origin(nces_website)
        host = _host(nces_website)
        if origin and host and nces_source:
            match = evidence.recorded_match(
                "website",
                origin,
                nces_source,
                "exact NCES district record published this organization website",
            )
            return SiteCandidate(origin, host, match, authoritative=True)
    for contact in db.contacts_for_lead(conn, int(lead["id"])):
        if not db.contact_is_page_verified(contact):
            continue
        domain = str(contact["official_domain"] or "").strip()
        source_url = str(contact["source_url"] or "").strip()
        origin = _origin(source_url) or (f"https://{domain}" if domain else "")
        if domain and origin:
            match = evidence.recorded_match(
                "website_candidate",
                origin,
                source_url,
                "website host inherited from a page-verified contact",
            )
            return SiteCandidate(origin, domain, match)
    entity = str(lead["entity_name"] or "")
    state = str(lead["state"] or "")
    for result in _search(f"{entity} {state} official website", limit=5, conn=conn):
        if _looks_official(entity, state, result):
            result_url = str(result.get("url") or "")
            host = _host(result_url)
            origin = _origin(result_url)
            if host and origin:
                excerpt = " ".join(
                    str(result.get(key) or "") for key in ("title", "description")
                ).strip()
                match = evidence.recorded_match(
                    "website_candidate", origin, result_url, excerpt[:500]
                )
                return SiteCandidate(origin, host, match)
    return None


def _extract_org(page_text: str, entity: str, source_url: str) -> dict[str, str]:
    """Claude reads ONE page; the caller verifies every field against that page."""
    client = Anthropic(**anthropic_client_options())
    prompt = (
        f'Below is a page from the official website of "{entity}". Extract the '
        "ORGANIZATION's own contact details (not a vendor's, not a person's private "
        "address). Use ONLY text on this page; copy each value EXACTLY as it appears. "
        "For general_email prefer a shared mailbox like info@/office@/contact@. "
        "Leave any field you cannot find on this page as an empty string.\n\n"
        'Respond with ONLY JSON: {"general_email": "...", "phone": "...", '
        '"street": "...", "city": "...", "state": "...", "postal_code": "..."}\n\n'
        f"PAGE ({source_url}):\n{page_text[:24000]}"
    )
    msg = client.messages.create(
        model=MODEL, max_tokens=300, messages=[{"role": "user", "content": prompt}]
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    try:
        return dict(json.loads(raw[raw.index("{") : raw.rindex("}") + 1]))
    except (ValueError, json.JSONDecodeError):
        return {}


def _merge(profile: OrgProfile, page_text: str, data: dict[str, str], url: str) -> None:
    """Fill one profile only from values proven on this exact page."""
    if profile.source_url and profile.source_url != url:
        raise ValueError("one organization profile cannot merge evidence across pages")
    accepted: dict[str, evidence.EvidenceMatch] = {}
    email = str(data.get("general_email") or "").strip()
    email_match = evidence.exact_email(page_text, email, url, field="general_email")
    if (
        not profile.general_email
        and email_match is not None
        and email.split("@", 1)[0].lower() in _GENERIC_LOCALPARTS
    ):
        profile.general_email = email
        accepted["general_email"] = email_match
    phone = str(data.get("phone") or "").strip()
    phone_match = evidence.phone(page_text, phone, url)
    if not profile.phone and phone_match is not None:
        profile.phone = phone
        accepted["phone"] = phone_match
    address_matches = evidence.address_block(
        page_text,
        {
            field_name: str(data.get(field_name) or "").strip()
            for field_name in ("street", "city", "state", "postal_code")
        },
        url,
    )
    for field_name, match in address_matches.items():
        if not getattr(profile, field_name):
            setattr(profile, field_name, match.value)
            accepted[field_name] = match
    if accepted:
        profile.source_url = url
        profile.field_evidence.update(accepted)


def _profile_score(profile: OrgProfile) -> tuple[int, int]:
    """Rank one-page profiles by useful verified facts, then address completeness."""
    values = (
        profile.general_email,
        profile.phone,
        profile.street,
        profile.city,
        profile.state,
        profile.postal_code,
    )
    return sum(bool(value) for value in values), sum(
        bool(value)
        for value in (profile.street, profile.city, profile.state, profile.postal_code)
    )


def _combine_profiles(candidates: list[OrgProfile], site: SiteCandidate) -> OrgProfile:
    """Project the best field values while retaining each field's exact page.

    The most complete page wins a duplicate field, then weaker pages fill gaps. The
    legacy single ``source_url`` is only a compatibility projection (prefer the
    general-mailbox page); truth-bearing consumers use ``field_evidence``.
    """
    combined = OrgProfile(website_candidate=site.origin, status="found")
    field_names = ("general_email", "phone")
    for candidate in sorted(candidates, key=_profile_score, reverse=True):
        for field_name in field_names:
            if getattr(combined, field_name) or not getattr(candidate, field_name):
                continue
            setattr(combined, field_name, getattr(candidate, field_name))
            combined.field_evidence[field_name] = candidate.field_evidence[field_name]
    # Address components are one compound fact. Copy them from exactly one page;
    # never fill a missing ZIP/state from a second address elsewhere on the site.
    address_fields = ("street", "city", "state", "postal_code")
    address_candidates = [
        candidate
        for candidate in candidates
        if any(getattr(candidate, field_name) for field_name in address_fields)
    ]
    if address_candidates:
        address = max(
            address_candidates,
            key=lambda candidate: sum(
                bool(getattr(candidate, field_name)) for field_name in address_fields
            ),
        )
        for field_name in address_fields:
            value = getattr(address, field_name)
            if value:
                setattr(combined, field_name, value)
                combined.field_evidence[field_name] = address.field_evidence[field_name]
    combined.website = site.origin if site.authoritative else ""
    anchor = combined.field_evidence.get("general_email") or next(
        iter(combined.field_evidence.values())
    )
    combined.source_url = anchor.source_url
    if site.authoritative:
        combined.field_evidence["website"] = site.evidence
    return combined


def evidenced_profile(conn: sqlite3.Connection, lead: sqlite3.Row) -> OrgProfile:
    """Return only current org projections proven by exact field evidence.

    This read-side gate protects user-facing and CRM consumers even if a legacy row
    or direct SQL edit populated compatibility columns without using
    :func:`db.save_org_profile`. Address components are all dropped if their current
    evidence cites more than one page.
    """
    lead_id = int(lead["id"])
    rows = list(
        conn.execute(
            """SELECT field_name,field_value,source_url
                 FROM organization_field_evidence
                WHERE lead_id=? AND status='current'""",
            (lead_id,),
        )
    )
    evidence_rows = {str(row["field_name"]): row for row in rows}
    columns = {
        "website": "org_website",
        "general_email": "org_general_email",
        "phone": "org_phone",
        "street": "org_street",
        "city": "org_city",
        "state": "org_state",
        "postal_code": "org_postal_code",
    }
    values: dict[str, str] = {}
    for field_name, column in columns.items():
        row = evidence_rows.get(field_name)
        projected = str(lead[column] or "")
        if row is not None and projected and str(row["field_value"]) == projected:
            values[field_name] = projected
    address_fields = ("street", "city", "state", "postal_code")
    address_sources = {
        str(evidence_rows[field_name]["source_url"])
        for field_name in address_fields
        if field_name in values
    }
    if len(address_sources) > 1:
        for field_name in address_fields:
            values.pop(field_name, None)
    source_row = evidence_rows.get("general_email")
    if source_row is None and evidence_rows:
        source_row = next(iter(evidence_rows.values()))
    return OrgProfile(
        website=values.get("website", ""),
        general_email=values.get("general_email", ""),
        phone=values.get("phone", ""),
        street=values.get("street", ""),
        city=values.get("city", ""),
        state=values.get("state", ""),
        postal_code=values.get("postal_code", ""),
        source_url=str(source_row["source_url"]) if source_row is not None else "",
        status="found" if values else "not_found",
    )


def enrich_org_profile(
    conn: sqlite3.Connection, lead_id: int, on_progress: Progress | None = None
) -> OrgProfile:
    """Scrape an org's site and persist verbatim-verified org details to the lead.

    Idempotent: a prior ``found`` profile is returned without re-scraping. A page
    that cannot be read raises SourceUnreachable-style outcome recorded as
    ``unreachable`` (retryable, nothing invented)."""
    from .. import db

    p = on_progress or _NOOP
    lead = db.get_lead(conn, lead_id)
    if lead is None:
        raise ValueError(f"unknown Grant lead id {lead_id}")
    current_evidence = conn.execute(
        """SELECT 1 FROM organization_field_evidence
           WHERE lead_id=? AND status='current' LIMIT 1""",
        (lead_id,),
    ).fetchone()
    if str(lead["org_profile_status"] or "") == "found" and current_evidence:
        return evidenced_profile(conn, lead)
    p("Looking up the organization's website")
    site = _resolve_site(conn, lead)
    if site is None:
        db.save_org_profile(conn, lead_id, OrgProfile(status="not_found"))
        return OrgProfile(status="not_found")
    entity = str(lead["entity_name"] or "")
    read_any = False
    candidates: list[OrgProfile] = []
    for path in _CONTACT_PATHS[:_MAX_PAGES]:
        url = f"{site.origin}{path}"
        p("Reading the organization's website")
        page_text = _scrape(url, conn=conn)
        if not page_text:
            continue
        read_any = True
        page_profile = OrgProfile(website_candidate=site.origin)
        _merge(page_profile, page_text, _extract_org(page_text, entity, url), url)
        if page_profile.field_evidence:
            candidates.append(page_profile)
    if not read_any:
        # We never actually read a page — honest retryable non-result.
        db.save_org_profile(conn, lead_id, OrgProfile(status="unreachable"))
        raise SourceUnreachable(f"could not read any page for {entity}")
    if not candidates:
        profile = OrgProfile(website_candidate=site.origin, status="not_found")
        db.save_org_profile(conn, lead_id, profile)
        return profile
    profile = _combine_profiles(candidates, site)
    db.save_org_profile(conn, lead_id, profile)
    return profile


def summarize_org_profile(profile: OrgProfile) -> str:
    """Render a previously obtained profile without causing another lookup."""
    if profile.status != "found":
        return " I couldn't verify the organization's contact details on its site."
    found: list[str] = []
    if profile.general_email:
        found.append(f"the organization's general email {profile.general_email}")
    if profile.phone:
        found.append(f"switchboard phone {profile.phone}")
    if profile.street or profile.city or profile.postal_code:
        address = ", ".join(
            part for part in (profile.street, profile.city, profile.postal_code) if part
        )
        found.append(f"address {address}")
    if profile.website:
        found.append(f"website {profile.website}")
    return " I also verified " + ", ".join(found) + "." if found else ""


def org_enrichment_summary(
    conn: sqlite3.Connection, lead_id: int, on_progress: Progress | None = None
) -> str:
    """Enrich the org profile and describe honestly what was added, for Grant.

    A network/extraction hiccup records nothing (retryable) and returns ''."""
    import sys
    import traceback

    try:
        profile = enrich_org_profile(conn, lead_id, on_progress)
    except SourceUnreachable as exc:
        # EXPECTED, already-handled non-result: the org's site couldn't be read
        # (blocked, offline, or no contact page). Nothing is recorded and it's
        # retryable, so log a clean one-liner — a full traceback here reads like a
        # code bug when it is just an unreachable website.
        print(
            f"[org-enrichment] site unreachable, nothing recorded ({exc})",
            file=sys.stderr,
        )
        return ""
    except Exception:  # noqa: BLE001 — an UNEXPECTED failure: keep the full traceback
        print("[tool-error] org_enrichment_summary:", file=sys.stderr)
        traceback.print_exc()
        return ""
    return summarize_org_profile(profile)
