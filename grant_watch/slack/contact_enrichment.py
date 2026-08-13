"""One lead's contact enrichment — verbatim-verified, with an escalation chain.

Split from tools.py to honor the 1000-line module cap. The chain (Chase's rule:
"every school and city has an email somewhere"): the org site's named person via
finder's verbatim gate, then a LinkedIn decision-maker, then the org's verified
general mailbox — and only when all three miss is a lead honestly not_found.
Every outcome is a typed ContactOutcome the batch search and single-lead tool
both consume; fallbacks persist what they found so Salesforce steps can build
on it. Organization enrichment is coordinated here exactly once per request so
rendering a result can never trigger a second paid lookup.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import db

if TYPE_CHECKING:
    from ..enrich.organization_profile import OrgProfile

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
      unreachable        — the source was down; NOTHING recorded, a retry re-attempts,
      needs_operator_retry — a PRIOR paid attempt cannot be proven spent or unspent,
                           so re-spending is refused. NOTHING was checked this time
                           and no absence was established: it is emphatically not
                           `not_found`, and every renderer must say so separately.
    """

    status: str
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    source_url: str = ""
    # The ORGANIZATION's main line, verified verbatim on its own page. Deliberately
    # a separate field from `phone`: `phone` belongs to the named person, and a
    # LinkedIn-sourced person has no phone of their own. Merging the two would put a
    # district switchboard next to a person's name and read as their direct number —
    # a rule-1 fabrication built out of two true facts.
    org_phone: str = ""
    # Pure, pre-rendered organization detail from the SAME enrichment pass. Keeping
    # it on the outcome prevents the Slack string wrapper from issuing a second
    # organization lookup merely to render an address or switchboard.
    org_summary: str = ""


def enrich_lead_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    on_progress: Progress | None = None,
    *,
    include_org_profile: bool = False,
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
        c for c in db.contacts_for_lead(conn, lead_id) if db.contact_is_page_verified(c)
    ]
    if existing:
        c = existing[0]
        org_summary = (
            _enrich_org_summary(conn, lead_id, on_progress)
            if include_org_profile
            else _stored_org_summary(conn, lead, lead_id)
        )
        if include_org_profile:
            lead = db.get_lead(conn, lead_id) or lead
        profile = _stored_org_profile(conn, lead, lead_id)
        return ContactOutcome(
            "verified",
            c["name"] or "",
            c["title"] or "",
            c["email"] or "",
            c["phone"] or "",
            c["source_url"] or "",
            profile.phone,
            org_summary,
        )
    if any(
        c["contact_status"] == "not_found" for c in db.contacts_for_lead(conn, lead_id)
    ):
        return ContactOutcome("not_found")
    recalled = _recall_prior_outcome(conn, lead, lead_id)
    if recalled is not None:
        return recalled

    def discover_bound() -> ContactOutcome:
        """Run every paid fallback only after the outer durable reservation."""
        candidate = finder.find_contact(
            str(lead["entity_name"]),
            str(lead["state"] or ""),
            on_progress=on_progress,
        )
        if candidate is None:
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
        org_summary = (
            _enrich_org_summary(conn, lead_id, on_progress)
            if include_org_profile
            else _stored_org_summary(conn, lead, lead_id)
        )
        refreshed_lead = (
            db.get_lead(conn, lead_id) or lead if include_org_profile else lead
        )
        profile = _stored_org_profile(conn, refreshed_lead, lead_id)
        return ContactOutcome(
            "verified",
            candidate.name,
            candidate.title,
            candidate.email,
            candidate.phone,
            candidate.source_url,
            profile.phone,
            org_summary,
        )

    def discover() -> ContactOutcome:
        """Bind every nested Firecrawl call to this lead's durable connection."""
        from ..enrich import firecrawl_gateway

        with firecrawl_gateway.bind_connection(conn, "contact_enrichment"):
            return discover_bound()

    from ..campaign import paid_calls
    from ..enrich import firecrawl_gateway

    try:
        return paid_calls.execute(
            conn,
            lead_id,
            "legacy_contact_enrichment",
            f"legacy-contact:{lead_id}",
            discover,
            # An unreachable source proves nothing was bought, so the attempt is
            # filed retryable instead of indeterminate. Without this a single 429
            # or timeout retired the lead permanently: `enrich_lead_contact` would
            # raise `IndeterminatePaidCall` on every later pass and the rep would
            # see `error` for a lead whose website came back minutes later.
            provably_unspent=(
                finder.SourceUnreachable,
                firecrawl_gateway.FirecrawlBudgetExhausted,
                firecrawl_gateway.FirecrawlBudgetNotConfigured,
            ),
        )
    except finder.SourceUnreachable:
        return ContactOutcome("unreachable")
    except paid_calls.IndeterminatePaidCall:
        # A lead burned before the fix above, or one whose spend genuinely cannot
        # be established. Either way this is NOT "no contact exists" — say so.
        return ContactOutcome("needs_operator_retry")
    except paid_calls.CompletedPaidCall:
        # Belt-and-braces behind _recall_prior_outcome: the ledger says this lead's
        # paid pass already ran, so re-spending is forbidden. Report whatever that
        # pass stored rather than an "error" cell that misreports a real success.
        # No durable evidenced outcome means only that the legacy row was incomplete
        # or quarantined. It cannot establish an exhaustive miss.
        return _recall_prior_outcome(conn, lead, lead_id) or ContactOutcome(
            "needs_operator_retry"
        )


def _best_linkedin_contact(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    """Pick the most useful stored LinkedIn contact, not whichever row came back first.

    A lead can accumulate SEVERAL linkedin_only rows: each enrichment pass writes what
    it found that day, and LinkedIn does not return the same person twice running. One
    production lead holds both a Teacher and an Assistant Superintendent for the same
    district, and which one a rep saw was decided by nothing more than row order. That
    is a lead-quality signal being thrown away by an implementation detail.

    Ranking is deliberately shallow and explainable: a title Monarch actually sells to
    beats any other title, any title beats none, and otherwise the later row wins.
    `contacts` has no timestamp column, so "later" means a higher id — reliable here
    because ids are gapless and assigned as max(rowid)+1, and stated rather than
    assumed. This CHOOSES between things Grant already verified; it never merges two
    rows into one person, which would invent a contact out of two true ones.
    """
    from ..enrich.zoominfo_enrichment import DECISION_MAKER_TITLES

    candidates = [row for row in rows if row["contact_status"] == "linkedin_only"]
    if not candidates:
        return None

    def rank(row: sqlite3.Row) -> tuple[int, int, int]:
        """Higher sorts better."""
        title = str(row["title"] or "").strip().lower()
        relevant = any(word in title for word in DECISION_MAKER_TITLES)
        return (1 if relevant else 0, 1 if title else 0, int(row["id"]))

    return max(candidates, key=rank)


def _recall_prior_outcome(
    conn: sqlite3.Connection, lead: sqlite3.Row, lead_id: int
) -> ContactOutcome | None:
    """Rebuild a completed FALLBACK outcome from the evidence that pass persisted.

    The paid-attempt ledger is keyed per lead, but the two guards above it only cover
    the `verified` and `not_found` endings. A lead whose first pass ended in a fallback
    (linkedin_only / linkedin_org_email / org_email) therefore fell through to
    paid_calls.execute on every later pass, raised CompletedPaidCall, and surfaced in
    the search grid as a bare "error" — reporting a failure for enrichment that had
    actually succeeded, and inviting a human to re-run work that can never re-run.

    Reconstruct instead, from exactly what the first pass wrote: the linkedin_only
    contact row and the organization profile columns. Returns None when neither
    exists, so a genuine first pass is never short-circuited.
    """
    linkedin = _best_linkedin_contact(db.contacts_for_lead(conn, lead_id))
    profile = _stored_org_profile(conn, lead, lead_id)
    general_email = profile.general_email
    profile_source = profile.source_url
    org_phone = profile.phone
    org_summary = _stored_org_summary(conn, lead, lead_id)
    if linkedin is not None and general_email:
        return ContactOutcome(
            "linkedin_org_email",
            str(linkedin["name"] or ""),
            str(linkedin["title"] or ""),
            general_email,
            "",
            str(linkedin["source_url"] or ""),
            org_phone,
            org_summary,
        )
    if linkedin is not None:
        return ContactOutcome(
            "linkedin_only",
            str(linkedin["name"] or ""),
            str(linkedin["title"] or ""),
            "",
            "",
            str(linkedin["source_url"] or ""),
            org_phone,
            org_summary,
        )
    if general_email:
        return ContactOutcome(
            "org_email",
            "",
            "",
            general_email,
            "",
            profile_source,
            org_phone,
            org_summary,
        )
    return None


def _stored_org_summary(
    conn: sqlite3.Connection, lead: sqlite3.Row, lead_id: int
) -> str:
    """Render stored facts without relabeling a legacy website as authoritative."""
    from ..enrich.organization_profile import summarize_org_profile

    profile = _stored_org_profile(conn, lead, lead_id)
    return summarize_org_profile(profile)


def _stored_org_profile(
    conn: sqlite3.Connection, lead: sqlite3.Row, lead_id: int
) -> OrgProfile:
    """Return the evidence-filtered org profile for this exact lead row."""
    from ..enrich.organization_profile import evidenced_profile

    if int(lead["id"]) != lead_id:
        raise ValueError("organization profile lead mismatch")
    return evidenced_profile(conn, lead)


def _enrich_org_summary(
    conn: sqlite3.Connection,
    lead_id: int,
    on_progress: Progress | None,
) -> str:
    """Run organization enrichment once and return a pure summary.

    An unavailable organization site does not erase an independently verified
    person. Unexpected failures remain loud and flow to the paid-attempt ledger.
    """
    from ..enrich.finder import SourceUnreachable
    from ..enrich.organization_profile import enrich_org_profile, summarize_org_profile

    try:
        profile = enrich_org_profile(conn, lead_id, on_progress)
    except SourceUnreachable:
        return ""
    return summarize_org_profile(profile)


def _fallback_contact(
    conn: sqlite3.Connection,
    lead: sqlite3.Row,
    lead_id: int,
    on_progress: Progress | None,
) -> ContactOutcome:
    """Escalate when no on-site person verifies: LinkedIn person, then org mailbox.

    Both steps persist what they honestly found (a linkedin_only contact row /
    the org profile columns) so later Salesforce steps can build on them. A clean
    negative and an unavailable source are distinct: only two clean negatives may
    become permanent ``not_found``."""
    from ..enrich import finder
    from ..enrich.organization_profile import enrich_org_profile, summarize_org_profile

    entity = str(lead["entity_name"] or "")
    state = str(lead["state"] or "")
    person: dict[str, str] | None = None
    person_unavailable = False
    try:
        person = finder.linkedin_person(entity, state, on_progress=on_progress)
    except finder.SourceUnreachable:
        person_unavailable = True
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
    org_phone = ""
    org_summary = ""
    profile_unavailable = False
    try:
        profile = enrich_org_profile(conn, lead_id, on_progress)
        general_email = profile.general_email
        profile_source = profile.source_url
        org_phone = profile.phone
        org_summary = summarize_org_profile(profile)
    except finder.SourceUnreachable:
        profile_unavailable = True
    # org_phone rides in its OWN field on every branch below. A LinkedIn-sourced
    # person has no phone of their own, so putting the switchboard in `phone` would
    # invent a direct line for exactly the people whose identity is least verified.
    if person is not None and general_email:
        return ContactOutcome(
            "linkedin_org_email",
            str(person["name"]),
            str(person.get("title") or ""),
            general_email,
            "",
            str(person["url"]),
            org_phone,
            org_summary,
        )
    if person is not None:
        return ContactOutcome(
            "linkedin_only",
            str(person["name"]),
            str(person.get("title") or ""),
            "",
            "",
            str(person["url"]),
            org_phone,
            org_summary,
        )
    if general_email:
        return ContactOutcome(
            "org_email",
            "",
            "",
            general_email,
            "",
            profile_source,
            org_phone,
            org_summary,
        )
    if person_unavailable or profile_unavailable:
        unavailable = []
        if person_unavailable:
            unavailable.append("LinkedIn search")
        if profile_unavailable:
            unavailable.append("organization website")
        raise finder.SourceUnreachable(" and ".join(unavailable) + " unavailable")
    db.mark_contact_not_found(conn, lead_id)
    return ContactOutcome("not_found", org_summary=org_summary)
