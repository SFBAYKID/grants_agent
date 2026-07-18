"""One lead's contact enrichment — verbatim-verified, with an escalation chain.

Split from tools.py to honor the 1000-line module cap. The chain (Chase's rule:
"every school and city has an email somewhere"): the org site's named person via
finder's verbatim gate, then a LinkedIn decision-maker, then the org's verified
general mailbox — and only when all three miss is a lead honestly not_found.
Every outcome is a typed ContactOutcome the batch search and single-lead tool
both consume; fallbacks persist what they found so Salesforce steps can build
on it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .. import db

# Progress callback: enrichment narrates slow steps into the Slack spinner.
Progress = Callable[[str], None]



@dataclass(frozen=True)
class ContactOutcome:
    """One lead's enrichment result — the honest, structured outcome the batch search
    and the single-lead tool both consume. status is exactly one of:
      verified           — a verbatim-verified contact (name/title/email populated),
      linkedin_org_email — LinkedIn person + the org's verified general mailbox,
      linkedin_only      — a LinkedIn person (profile URL, no email claimed),
      org_email          — only the org's verified general mailbox, no named person,
      not_found          — person, LinkedIn, AND org mailbox all came up empty,
      unreachable        — the source was down; NOTHING recorded, a retry re-attempts.
    """

    status: str
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    source_url: str = ""


def enrich_lead_contact(
    conn: sqlite3.Connection, lead_id: int, on_progress: Progress | None = None
) -> ContactOutcome:
    """Find + persist ONE lead's best contact through finder's verbatim gate, reusing a
    caller-supplied connection so a batch enriches on a single handle. Idempotent: an
    existing verified contact is returned without re-scraping. A SourceUnreachable
    outage records nothing (retryable) and is NEVER written as not_found."""
    from ..enrich import finder  # local import: keeps poll and status paths light

    lead = db.get_lead(conn, lead_id)
    if lead is None:
        raise ValueError(f"unknown Grant lead id {lead_id}")
    existing = [
        c
        for c in db.contacts_for_lead(conn, lead_id)
        if c["contact_status"] == "verified"
    ]
    if existing:
        c = existing[0]
        return ContactOutcome(
            "verified",
            c["name"] or "",
            c["title"] or "",
            c["email"] or "",
            c["phone"] or "",
            c["source_url"] or "",
        )
    try:
        candidate = finder.find_contact(
            str(lead["entity_name"]), str(lead["state"] or ""), on_progress=on_progress
        )
    except finder.SourceUnreachable:
        return ContactOutcome("unreachable")  # could not look -> record nothing
    if candidate is None:
        # Fallback chain (Chase's rule: every school and city has an email
        # somewhere). No named person on the site -> try LinkedIn for a named
        # decision-maker, then the org's verified general mailbox. Only when
        # ALL of those come up empty is the lead honestly not_found.
        return _fallback_contact(conn, lead, lead_id, on_progress)
    db.save_contact(
        conn,
        lead_id,
        candidate.name,
        candidate.title,
        candidate.email,
        candidate.phone,
        candidate.source_url,
        candidate.confidence,
        candidate.official_domain,
        candidate.field_evidence,
    )
    return ContactOutcome(
        "verified",
        candidate.name,
        candidate.title,
        candidate.email,
        candidate.phone,
        candidate.source_url,
    )


def _fallback_contact(
    conn: sqlite3.Connection,
    lead: sqlite3.Row,
    lead_id: int,
    on_progress: Progress | None,
) -> ContactOutcome:
    """Escalate when no on-site person verifies: LinkedIn person, then org mailbox.

    Both steps persist what they honestly found (a linkedin_only contact row /
    the org profile columns) so later Salesforce steps can build on them. Every
    failure degrades silently to the next step — fallbacks never raise."""
    from ..enrich import finder
    from ..enrich.organization_profile import enrich_org_profile

    entity = str(lead["entity_name"] or "")
    state = str(lead["state"] or "")
    person: dict[str, str] | None = None
    try:
        person = finder.linkedin_person(entity, state, on_progress=on_progress)
    except Exception:  # noqa: BLE001 — a fallback miss is a miss, not a crash
        person = None
    if person is not None:
        title = str(person.get("title") or "")
        if (
            db.canonical_entity_key(title).partition("|")[0]
            == db.canonical_entity_key(entity).partition("|")[0]
        ):
            title = ""  # the org name in the title slot is no title at all
        db.save_linkedin_contact(
            conn, lead_id, str(person["name"]), title, str(person["url"])
        )
    general_email = ""
    profile_source = ""
    try:
        profile = enrich_org_profile(conn, lead_id, on_progress)
        general_email = profile.general_email
        profile_source = profile.source_url
    except Exception:  # noqa: BLE001 — org-profile misses degrade honestly too
        general_email = ""
    if person is not None and general_email:
        return ContactOutcome(
            "linkedin_org_email",
            str(person["name"]),
            str(person.get("title") or ""),
            general_email,
            "",
            str(person["url"]),
        )
    if person is not None:
        return ContactOutcome(
            "linkedin_only",
            str(person["name"]),
            str(person.get("title") or ""),
            "",
            "",
            str(person["url"]),
        )
    if general_email:
        return ContactOutcome(
            "org_email", "", "", general_email, "", profile_source
        )
    db.mark_contact_not_found(conn, lead_id)
    return ContactOutcome("not_found")
