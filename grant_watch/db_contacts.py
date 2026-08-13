"""Contact persistence — one function per EVIDENCE CLASS, never one generic writer.

The split is deliberate and load-bearing. `contacts.contact_status` decides whether a
record can reach an outreach brief or a rich card, so a single writer taking that
status as a parameter would put the whole honesty model one string literal away from
collapse. Each function here hard-codes the class it is entitled to write:

  save_contact                 page-verified — finder saw it verbatim on the org's page
  save_vendor_contact          licensed vendor data; nobody checked it
  save_human_asserted_contact  a named rep said so, in a thread, on a date
  save_linkedin_contact        a profile Grant found; ownership unproven
  mark_contact_not_found       the honest empty result

Split from db.py at the 1000-line cap (rule 4); all are re-exported from db.py, so
every `db.<name>` call site is unchanged.
"""

from __future__ import annotations

import json
import sqlite3

from .db_common import _now


# ---------------------------------------------------------------- contacts (Phase 2)


def _field_evidence_json(field_evidence: dict[str, object] | None) -> str | None:
    """Serialize complete typed contact evidence or refuse an untyped claim."""
    if not field_evidence:
        raise ValueError("verified contact requires typed email and name evidence")
    payload: dict[str, dict[str, str]] = {}
    for field_name, match in field_evidence.items():
        values = {
            "field": str(getattr(match, "field", "")),
            "value": str(getattr(match, "value", "")),
            "source_url": str(getattr(match, "source_url", "")),
            "excerpt": str(getattr(match, "excerpt", ""))[:500],
            "evidence_hash": str(getattr(match, "evidence_hash", "")),
            "verifier_version": str(getattr(match, "verifier_version", "")),
        }
        if values["field"] != field_name or not all(values.values()):
            raise ValueError(f"contact field {field_name!r} lacks typed evidence")
        payload[field_name] = values
    return json.dumps(payload, sort_keys=True)


def _verified_contact_evidence_json(
    name: str,
    title: str,
    email: str,
    phone: str,
    source_url: str,
    field_evidence: dict[str, object] | None,
) -> str:
    """Require typed proof for every fact written as page-verified.

    A verified person must have exact name and email evidence from the same source
    page. Optional title and phone values are persisted only when their own evidence
    is present too. This is the write boundary that prevents a direct caller from
    bypassing finder's parser with an untyped boolean or a legacy status string.
    """
    serialized = _field_evidence_json(field_evidence)
    assert serialized is not None
    payload = json.loads(serialized)
    required = {"name": name.strip(), "email": email.strip()}
    for field_name, expected in required.items():
        item = payload.get(field_name)
        if not expected or not isinstance(item, dict):
            raise ValueError("verified contact requires typed email and name evidence")
        actual = str(item.get("value") or "").strip()
        values_match = (
            actual.lower() == expected.lower()
            if field_name == "email"
            else " ".join(actual.split()) == " ".join(expected.split())
        )
        if not values_match or str(item.get("source_url") or "") != source_url:
            raise ValueError(
                f"verified contact {field_name} evidence does not match the row"
            )
    for field_name, expected in (("title", title), ("phone", phone)):
        if not expected.strip():
            continue
        item = payload.get(field_name)
        if not isinstance(item, dict) or str(item.get("value") or "").strip() != (
            expected.strip()
        ):
            raise ValueError(
                f"verified contact {field_name} lacks matching typed evidence"
            )
        if str(item.get("source_url") or "") != source_url:
            raise ValueError(
                f"verified contact {field_name} evidence does not match the row"
            )
    return serialized


def contact_is_page_verified(contact: sqlite3.Row) -> bool:
    """Return whether a stored row still has exact complete typed person evidence.

    Migration/trigger guards are the primary invariant. This read-side predicate is
    defense in depth for external-action consumers and for any read-only legacy copy
    opened before its migration has run.
    """
    if str(contact["contact_status"] or "") != "verified":
        return False
    try:
        payload = json.loads(str(contact["field_evidence_json"] or ""))
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    source_url = str(contact["source_url"] or "")
    expected = {
        "name": str(contact["name"] or "").strip(),
        "email": str(contact["email"] or "").strip(),
    }
    for optional in ("title", "phone"):
        value = str(contact[optional] or "").strip()
        if value:
            expected[optional] = value
    for field_name, value in expected.items():
        item = payload.get(field_name)
        if not value or not isinstance(item, dict):
            return False
        actual = str(item.get("value") or "").strip()
        equal = (
            actual.lower() == value.lower()
            if field_name == "email"
            else " ".join(actual.split()) == " ".join(value.split())
        )
        if not equal or str(item.get("field") or "") != field_name:
            return False
        if str(item.get("source_url") or "") != source_url:
            return False
        if any(
            not str(item.get(key) or "").strip()
            for key in ("excerpt", "evidence_hash", "verifier_version")
        ):
            return False
    return True


def save_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str,
    title: str,
    email: str,
    phone: str,
    source_url: str,
    confidence: str,
    official_domain: str = "",
    field_evidence: dict[str, object] | None = None,
) -> int:
    """Store a fully evidenced VERIFIED contact and return its local id."""
    evidence_json = _verified_contact_evidence_json(
        name, title, email, phone, source_url, field_evidence
    )
    cur = conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,source_url,confidence,contact_status,
              official_domain,field_evidence_json,provenance)
           VALUES (?,?,?,?,?,?,?,'verified',?,?,'page_verified')""",
        (
            lead_id,
            name,
            title,
            email,
            phone,
            source_url,
            confidence,
            official_domain or None,
            evidence_json,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_vendor_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str,
    title: str,
    email: str,
    phone: str,
    vendor_person_id: str,
    *,
    do_not_call: bool,
    mobile_phone: str = "",
) -> int:
    """Store a LICENSED-VENDOR contact — never 'verified', whatever the vendor claims.

    Distinct from save_contact because the evidence class differs and the difference
    is load-bearing. finder's gate proves an email by finding it verbatim on the
    organization's own page; a purchased record has no such page and cannot be
    checked, so it gets its own status that no `== 'verified'` comparison can match.
    That single fact is what keeps vendor data out of the Persequor brief and out of
    a rich card.

    A do-not-call flagged number is NOT stored in `phone` at all. Callers must not
    "keep it for reference": every consumer of contacts.phone treats it as dialable,
    and salesforce_contact_records copies it straight into a Lead's Phone field.

    `mobile_phone` is kept SEPARATE from `phone` for the same reason the person's
    line is kept separate from the organization's: they are different facts about
    how to reach somebody, and Salesforce has a distinct field for each. Collapsing
    them put a mobile into a Lead's Phone field, where every rep reads it as a desk
    line.
    """
    cur = conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,mobile_phone,source_url,confidence,
              contact_status,contact_provenance,provenance,do_not_call,
              vendor_person_id)
           VALUES (?,?,?,?,?,?,'','medium','vendor_licensed','vendor_licensed',
                   'vendor_licensed',?,?)""",
        (
            lead_id,
            name,
            title,
            email,
            "" if do_not_call else phone,
            # A do-not-call flag covers every number for that person, mobile
            # included — withholding the desk line and keeping the mobile would
            # defeat the whole point of honouring it.
            "" if do_not_call else mobile_phone,
            1 if do_not_call else 0,
            vendor_person_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_human_asserted_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    name: str = "",
    title: str = "",
    email: str = "",
    phone: str = "",
    asserted_by: str,
) -> tuple[int, list[str]]:
    """Record a contact fact a NAMED REP supplied, ALWAYS as its own row.

    Returns (contact id, list of fields actually written).

    Grant used to refuse these outright, which was the honesty rule applied to the
    wrong case: it exists to stop Grant INVENTING a contact and presenting it as
    discovered, not to stop a human telling Grant something true. A rep who types a
    number is the authority on it, so the honest response is to record WHO said it
    and WHEN. Attribution is the mechanism, not refusal.

    THIS NEVER EDITS AN EXISTING ROW, and that is the whole safety property. An
    earlier version filled empty fields on the contact the rep named, which left the
    row still reading `contact_status='verified'` while now carrying a value nobody
    verified — a rep-typed email was proven to reach the Persequor outreach brief
    that way, because grant.py selects the brief's contact on exactly that status.
    A separate row cannot do that: `human_asserted` matches no `== 'verified'`
    comparison anywhere. It also keeps attribution truthful, since
    `asserted_by_slack_user` is per ROW — after an in-place edit you could no longer
    tell which field the human supplied.
    """
    now = _now()
    written = [
        label
        for label, value in (
            ("name", name),
            ("title", title),
            ("email", email),
            ("phone", phone),
        )
        if value
    ]
    cur = conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,source_url,confidence,contact_status,
              contact_provenance,provenance,asserted_by_slack_user,asserted_at)
           VALUES (?,?,?,?,?,'','medium','human_asserted',NULL,'human_asserted',?,?)""",
        (lead_id, name, title, email, phone, asserted_by, now),
    )
    conn.commit()
    return int(cur.lastrowid), written


def save_linkedin_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str,
    title: str,
    profile_url: str,
) -> int:
    """Store a LinkedIn-sourced person: name/title/profile only, never an email.

    Distinct from save_contact because the evidence class differs — a profile's
    ownership is not verified, so contact_status is 'linkedin_only'."""
    cur = conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,source_url,confidence,contact_status,
              provenance)
           VALUES (?,?,?,NULL,NULL,?,'medium','linkedin_only','linkedin_claimed')""",
        (lead_id, name, title, profile_url),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_contact_not_found(conn: sqlite3.Connection, lead_id: int) -> None:
    """The honest outcome when enrichment finds nothing verifiable."""
    conn.execute(
        "INSERT INTO contacts (lead_id, contact_status) VALUES (?, 'not_found')",
        (lead_id,),
    )
    conn.commit()
