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
import os
import re
import sqlite3
from dataclasses import dataclass

from anthropic import Anthropic

from .finder import (
    MODEL,
    Progress,
    SourceUnreachable,
    _NOOP,
    _EMAIL_RE,
    _host,
    _looks_official,
    _phone_on_page,
    _scrape,
    _search,
    _text_field_on_page,
    verify_on_page,
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
    """Org-level facts, each verified verbatim on ``source_url`` (or blank)."""

    website: str = ""
    general_email: str = ""
    phone: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    source_url: str = ""
    status: str = "not_found"  # found | not_found | unreachable


def _general_email_on_page(page_text: str, email: str) -> bool:
    """A general org email must appear verbatim; unlike a person's, it needs no name.

    It must also read like a shared mailbox (info@/office@/…), so a stray
    personal address on the page is not mistaken for the organization's."""
    if not email or not _EMAIL_RE.fullmatch(email):
        return False
    if email.lower() not in page_text.lower():
        return False
    localpart = email.split("@", 1)[0].lower()
    return any(localpart == generic or localpart.startswith(generic) for generic in _GENERIC_LOCALPARTS)


def _resolve_site(conn: sqlite3.Connection, lead: sqlite3.Row) -> str:
    """Find the organization's official host — from a verified contact or search."""
    from .. import db

    for contact in db.contacts_for_lead(conn, int(lead["id"])):
        domain = str(contact["official_domain"] or "").strip()
        if domain:
            return domain
    entity = str(lead["entity_name"] or "")
    state = str(lead["state"] or "")
    for result in _search(f"{entity} {state} official website", limit=5):
        if _looks_official(entity, state, result):
            host = _host(str(result.get("url") or ""))
            if host:
                return host
    return ""


def _extract_org(page_text: str, entity: str, source_url: str) -> dict[str, str]:
    """Claude reads ONE page; the caller verifies every field against that page."""
    client = Anthropic()
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
    """Fill any still-blank profile field with a value verified on THIS page."""
    if not profile.source_url:
        profile.source_url = url
    email = str(data.get("general_email") or "").strip()
    if not profile.general_email and _general_email_on_page(page_text, email):
        profile.general_email = email
    phone = str(data.get("phone") or "").strip()
    if not profile.phone and _phone_on_page(page_text, phone):
        profile.phone = phone
    street = str(data.get("street") or "").strip()
    if not profile.street and _text_field_on_page(page_text, street):
        profile.street = street
    city = str(data.get("city") or "").strip()
    if not profile.city and _text_field_on_page(page_text, city):
        profile.city = city
    state = str(data.get("state") or "").strip()
    if not profile.state and _text_field_on_page(page_text, state):
        profile.state = state
    postal = str(data.get("postal_code") or "").strip()
    if not profile.postal_code and re.fullmatch(r"\d{5}(?:-\d{4})?", postal) and postal in page_text:
        profile.postal_code = postal


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
    if str(lead["org_profile_status"] or "") == "found":
        return OrgProfile(
            website=str(lead["org_website"] or ""),
            general_email=str(lead["org_general_email"] or ""),
            phone=str(lead["org_phone"] or ""),
            street=str(lead["org_street"] or ""),
            city=str(lead["org_city"] or ""),
            state=str(lead["org_state"] or ""),
            postal_code=str(lead["org_postal_code"] or ""),
            source_url=str(lead["org_profile_source_url"] or ""),
            status="found",
        )
    p("Looking up the organization's website")
    host = _resolve_site(conn, lead)
    if not host:
        db.save_org_profile(conn, lead_id, OrgProfile(status="not_found"))
        return OrgProfile(status="not_found")
    profile = OrgProfile(website=f"https://{host}")
    entity = str(lead["entity_name"] or "")
    read_any = False
    for path in _CONTACT_PATHS[:_MAX_PAGES]:
        url = f"https://{host}{path}"
        p("Reading the organization's website")
        page_text = _scrape(url)
        if not page_text:
            continue
        read_any = True
        _merge(profile, page_text, _extract_org(page_text, entity, url), url)
        if profile.street and profile.general_email and profile.phone:
            break
    if not read_any:
        # We never actually read a page — honest retryable non-result.
        db.save_org_profile(conn, lead_id, OrgProfile(status="unreachable"))
        raise SourceUnreachable(f"could not read any page for {entity}")
    profile.status = "found" if (profile.street or profile.general_email) else "not_found"
    db.save_org_profile(conn, lead_id, profile)
    return profile


def org_enrichment_summary(
    conn: sqlite3.Connection, lead_id: int, on_progress: Progress | None = None
) -> str:
    """Enrich the org profile and describe honestly what was added, for Grant.

    A network/extraction hiccup records nothing (retryable) and returns ''."""
    import sys
    import traceback

    try:
        profile = enrich_org_profile(conn, lead_id, on_progress)
    except Exception:  # noqa: BLE001 — any failure is a retryable non-result
        print("[tool-error] org_enrichment_summary:", file=sys.stderr)
        traceback.print_exc()
        return ""
    found: list[str] = []
    if profile.general_email:
        found.append(f"the organization's general email {profile.general_email}")
    if profile.phone:
        found.append(f"phone {profile.phone}")
    if profile.street or profile.city or profile.postal_code:
        address = ", ".join(
            part
            for part in (profile.street, profile.city, profile.postal_code)
            if part
        )
        found.append(f"address {address}")
    if profile.website:
        found.append(f"website {profile.website}")
    if not found:
        return " I couldn't verify the organization's address or general email on its site."
    return " From the organization's website I also added " + "; ".join(found) + "."
