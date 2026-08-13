"""Fresh contact-evidence lifecycle for rich-card preparation.

The paid discovery callback is wrapped by ``paid_calls.execute`` before any network
operation. Successful re-verification supersedes prior evidence; a genuine completed
miss records removed/not-found, while an outage leaves prior evidence untouched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..enrich import evidence, finder
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
    field_evidence: dict[str, evidence.EvidenceMatch] | None = None


Finder = Callable[[sqlite3.Row], ContactFact | None]


def _default_finder(
    lead: sqlite3.Row, conn: sqlite3.Connection | None = None
) -> ContactFact | None:
    """Prefer re-verifiable official mailbox, then find a named work contact."""
    if conn is not None:
        from ..enrich.organization_profile import evidenced_profile

        profile = evidenced_profile(conn, lead)
        general_email = profile.general_email
        general_url = profile.source_url
        evidenced_website = profile.website
    else:
        general_email = ""
        general_url = ""
        evidenced_website = ""
    trusted_website = (
        str(lead["nces_website"] or "")
        if str(lead["nces_website_status"] or "") == "verified"
        else evidenced_website
    )
    official_domain = finder._host(trusted_website)
    general_match = (
        finder.general_mailbox_evidence(general_email, general_url, official_domain)
        if general_email and general_url
        else None
    )
    if general_match is not None:
        return ContactFact(
            "official_general",
            "",
            "",
            general_email,
            general_url,
            official_domain,
            {"email": general_match},
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
        candidate.field_evidence,
    )


def fact_hash(fact: ContactFact) -> str:
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


def serialize_fact_evidence(fact: ContactFact) -> str:
    """Serialize exact typed proof for every rich contact field being asserted."""
    required = {"email": fact.email.strip()}
    if fact.contact_type == "named_direct":
        required["name"] = fact.name.strip()
    elif fact.contact_type != "official_general":
        raise ValueError("unsupported rich contact evidence type")
    if fact.title.strip():
        required["title"] = fact.title.strip()
    if fact.contact_type == "official_general" and (
        fact.name.strip() or fact.title.strip()
    ):
        raise ValueError("an organization mailbox cannot assert a named person")
    supplied = fact.field_evidence or {}
    payload: dict[str, dict[str, str]] = {}
    for field_name, expected in required.items():
        match = supplied.get(field_name)
        values = {
            "field": str(getattr(match, "field", "")),
            "value": str(getattr(match, "value", "")).strip(),
            "source_url": str(getattr(match, "source_url", "")),
            "excerpt": str(getattr(match, "excerpt", ""))[:500],
            "evidence_hash": str(getattr(match, "evidence_hash", "")),
            "verifier_version": str(getattr(match, "verifier_version", "")),
        }
        equal = (
            values["value"].lower() == expected.lower()
            if field_name == "email"
            else " ".join(values["value"].split()) == " ".join(expected.split())
        )
        if (
            not expected
            or values["field"] != field_name
            or not equal
            or values["source_url"] != fact.evidence_url
            or not all(values.values())
        ):
            raise ValueError(
                f"rich contact {field_name} requires exact typed page evidence"
            )
        payload[field_name] = values
    return json.dumps(payload, sort_keys=True)


def contact_fact_is_verified(row: sqlite3.Row) -> bool:
    """Validate a persisted rich contact independently of a legacy status label."""
    if str(row["status"] or "") != "verified":
        return False
    try:
        fact = ContactFact(
            contact_type=str(row["contact_type"] or ""),
            name=str(row["name"] or ""),
            title=str(row["title"] or ""),
            email=str(row["email"] or ""),
            evidence_url=str(row["official_evidence_url"] or ""),
            official_domain=str(row["official_domain"] or ""),
            field_evidence=None,
        )
        payload = json.loads(str(row["field_evidence_json"] or ""))
    except (IndexError, KeyError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    required = {"email": fact.email.strip()}
    if fact.contact_type == "named_direct":
        required["name"] = fact.name.strip()
    elif (
        fact.contact_type != "official_general"
        or fact.name.strip()
        or fact.title.strip()
    ):
        return False
    if fact.title.strip():
        required["title"] = fact.title.strip()
    if not fact.evidence_url or not fact.official_domain:
        return False
    for field_name, expected in required.items():
        item = payload.get(field_name)
        if not expected or not isinstance(item, dict):
            return False
        actual = str(item.get("value") or "").strip()
        equal = (
            actual.lower() == expected.lower()
            if field_name == "email"
            else " ".join(actual.split()) == " ".join(expected.split())
        )
        if (
            str(item.get("field") or "") != field_name
            or not equal
            or str(item.get("source_url") or "") != fact.evidence_url
            or any(
                not str(item.get(key) or "").strip()
                for key in ("excerpt", "evidence_hash", "verifier_version")
            )
        ):
            return False
    return str(row["evidence_hash"] or "") == fact_hash(fact)


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
    if latest is not None and contact_fact_is_verified(latest):
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
        from ..enrich import firecrawl_gateway

        with firecrawl_gateway.allow_indeterminate_retry(retry_indeterminate):
            with firecrawl_gateway.bind_connection(conn, "rich_contact_refresh"):
                fact = (
                    _default_finder(lead, conn)
                    if finder_fn is _default_finder
                    else finder_fn(lead)
                )
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
            field_evidence_json = serialize_fact_evidence(fact)
            first_verified = (
                str(latest["first_verified_at"])
                if latest is not None and latest["email"] == fact.email
                else at.isoformat()
            )
            conn.execute(
                """INSERT INTO contact_evidence
                     (id,lead_id,status,contact_type,name,title,email,
                      official_evidence_url,official_domain,evidence_hash,
                      first_verified_at,last_checked_at,last_verified_at,expires_at,
                      field_evidence_json)
                   VALUES (?,?,'verified',?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    lead_id,
                    fact.contact_type,
                    fact.name or None,
                    fact.title or None,
                    fact.email,
                    fact.evidence_url,
                    fact.official_domain,
                    fact_hash(fact),
                    first_verified,
                    at.isoformat(),
                    at.isoformat(),
                    (at + timedelta(days=CONTACT_FRESH_DAYS)).isoformat(),
                    field_evidence_json,
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
