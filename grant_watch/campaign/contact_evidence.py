"""Fresh contact-evidence lifecycle for rich-card preparation.

The paid discovery callback is wrapped by ``paid_calls.execute`` before any network
operation. Successful re-verification supersedes prior evidence; a genuine completed
miss records removed/not-found, while an outage leaves prior evidence untouched.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..enrich import finder
from . import paid_calls
from .policy import CONTACT_FRESH_DAYS


@dataclass(frozen=True)
class ContactFact:
    """One public official contact proven by a preparation provider."""

    contact_type: str
    name: str
    title: str
    email: str
    evidence_url: str
    official_domain: str


Finder = Callable[[sqlite3.Row], ContactFact | None]


def _default_finder(lead: sqlite3.Row) -> ContactFact | None:
    """Prefer re-verifiable official mailbox, then find a named work contact."""
    general_email = str(lead["org_general_email"] or "")
    general_url = str(lead["org_profile_source_url"] or "")
    official_domain = finder._host(str(lead["org_website"] or ""))
    if (
        general_email
        and general_url
        and finder.reverify_general_mailbox(general_email, general_url, official_domain)
    ):
        return ContactFact(
            "official_general", "", "", general_email, general_url, official_domain
        )
    candidate = finder.find_contact(str(lead["entity_name"]), str(lead["state"] or ""))
    if candidate is None:
        return None
    return ContactFact(
        "named_direct",
        candidate.name,
        candidate.title,
        candidate.email,
        candidate.source_url,
        candidate.official_domain,
    )


def _hash(fact: ContactFact) -> str:
    """Hash the exact evidence locator and verified public contact facts."""
    payload = "|".join(
        (
            fact.contact_type,
            fact.name,
            fact.title,
            fact.email.lower(),
            fact.evidence_url,
            fact.official_domain.lower(),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def refresh(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    finder_fn: Finder = _default_finder,
    retry_indeterminate: bool = False,
    now: datetime | None = None,
) -> str:
    """Refresh one contact with durable paid-call state; return lifecycle status."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if lead is None:
        raise ValueError(f"unknown lead {lead_id}")
    latest = conn.execute(
        """SELECT * FROM contact_evidence WHERE lead_id=?
           ORDER BY last_checked_at DESC,rowid DESC LIMIT 1""",
        (lead_id,),
    ).fetchone()
    if latest is not None and latest["status"] == "verified":
        try:
            expires = datetime.fromisoformat(
                str(latest["expires_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            expires = at
        if expires > at:
            return "fresh"
    request_key = f"rich-contact:{lead_id}:{at.date().isoformat()}"

    def discover_and_persist() -> str:
        """Run paid discovery and atomically persist its definite evidence outcome."""
        fact = finder_fn(lead)
        with conn:
            if latest is not None and latest["status"] == "verified":
                conn.execute(
                    "UPDATE contact_evidence SET status='superseded' WHERE id=?",
                    (latest["id"],),
                )
            if fact is None:
                status = "removed" if latest is not None else "not_found"
                conn.execute(
                    """INSERT INTO contact_evidence
                         (id,lead_id,status,last_checked_at)
                       VALUES (?,?,?,?)""",
                    (uuid.uuid4().hex, lead_id, status, at.isoformat()),
                )
                return status
            first_verified = (
                str(latest["first_verified_at"])
                if latest is not None and latest["email"] == fact.email
                else at.isoformat()
            )
            conn.execute(
                """INSERT INTO contact_evidence
                     (id,lead_id,status,contact_type,name,title,email,
                      official_evidence_url,official_domain,evidence_hash,
                      first_verified_at,last_checked_at,last_verified_at,expires_at)
                   VALUES (?,?,'verified',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    lead_id,
                    fact.contact_type,
                    fact.name or None,
                    fact.title or None,
                    fact.email,
                    fact.evidence_url,
                    fact.official_domain,
                    _hash(fact),
                    first_verified,
                    at.isoformat(),
                    at.isoformat(),
                    (at + timedelta(days=CONTACT_FRESH_DAYS)).isoformat(),
                ),
            )
        return "verified"

    return paid_calls.execute(
        conn,
        lead_id,
        "contact_refresh",
        request_key,
        discover_and_persist,
        retry_indeterminate=retry_indeterminate,
    )
