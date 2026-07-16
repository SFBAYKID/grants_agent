"""Deterministic, tenant-local routing helpers for Slack CRM requests."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import db

_CRM_ACTION_RE = re.compile(
    r"<grant-crm-action>(\{.*?\})</grant-crm-action>", re.DOTALL)


def _extract_pending_action(text: str) -> tuple[str, dict[str, str] | None]:
    """Remove a server-only CRM marker and return validated button metadata."""
    match = _CRM_ACTION_RE.search(text)
    if match is None:
        return text, None
    clean = _CRM_ACTION_RE.sub("", text).strip()
    try:
        value = json.loads(match.group(1))
        action = {
            "action_id": str(value["action_id"]),
            "nonce": str(value["nonce"]),
            "preview": str(value["preview"]),
            "expires_at": str(value["expires_at"]),
        }
    except (KeyError, TypeError, json.JSONDecodeError):
        return clean, None
    return clean, action


def _explicit_lead_creation_request(
        text: str, thread_context: list[str] | None = None) -> bool:
    """Recognize a direct request for one standalone Salesforce Lead."""
    normalized = " ".join(text.lower().replace("stand-alone", "standalone").split())
    if re.search(r"\b(?:campaign|opportunit(?:y|ies))\b", normalized):
        return False
    if re.search(r"\bstand\s*alone\s+lead\b|\bstandalone\s+lead\b", normalized):
        return True
    if re.search(r"\bcreate\s+it\s+anyway\b", normalized):
        return any("lead" in item.lower() for item in (thread_context or [])[-3:])
    action = bool(re.search(r"\b(create|add|put|make|prepare)\b", normalized))
    action = action or ("show" in normalized.split() and "preview" in normalized.split())
    target = bool(re.search(r"\b(?:salesforce|lead)\b", normalized))
    return action and target


def _lead_ids(value: str) -> set[int]:
    """Return every explicit Grant lead number in one user turn."""
    return {int(match) for match in re.findall(
        r"\bgrant\s+leads?\s*#?\s*(\d+)\b", value, re.IGNORECASE)}


def _explicit_grant_lead_id(
        text: str, thread_context: list[str] | None = None) -> int | None:
    """Resolve one unambiguous ``Grant lead N`` from rep-authored context only."""
    prior_rep_turns = [value for value in (thread_context or [])[-10:][::-1]
                       if value.casefold().startswith("rep:")]
    for value in [text, *prior_rep_turns]:
        ids = _lead_ids(value)
        if len(ids) > 1:
            return None
        if len(ids) == 1:
            return next(iter(ids))
    return None


def _load_referenced_lead(
        text: str, thread_context: list[str] | None) -> sqlite3.Row | None:
    """Load only the lead explicitly named in this tenant-local conversation."""
    lead_id = _explicit_grant_lead_id(text, thread_context)
    if lead_id is None:
        return None
    conn = db.connect()
    try:
        return db.get_lead(conn, lead_id)
    finally:
        conn.close()


def _is_organization_enrichment_request(text: str) -> bool:
    """Recognize filling blank organization fields on an existing Lead."""
    normalized = " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())
    if _is_person_target_request(text):
        return False
    action = any(word in normalized.split() for word in (
        "update", "fill", "enrich", "complete", "populate", "repair"))
    fields = any(word in normalized for word in (
        "address", "street", "city", "postal", "country", "website", "phone", "notes"))
    return "salesforce" in normalized and "lead" in normalized and action and fields


def _requests_person_without_verified_email(text: str) -> bool:
    """Recognize a selected person whose absent email must remain blank."""
    normalized = " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())
    person = any(phrase in normalized for phrase in (
        "the person is", "identified person", "this person", "linkedin person"))
    missing_email = any(phrase in normalized for phrase in (
        "no verified email", "without an email", "no email", "email is unavailable"))
    missing_email = missing_email or ("email" in normalized and "blank" in normalized)
    return (person or _is_person_target_request(text)) and missing_email


def _is_person_target_request(text: str) -> bool:
    """Return whether the rep explicitly selected a person rather than an organization."""
    normalized = " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())
    return (
        "linkedin" in normalized
        or "exact person" in normalized
        or ("email" in normalized and "blank" in normalized)
    )


def _requested_person_name(text: str) -> str:
    """Extract only the user-named person so evidence must bind to that name."""
    match = re.search(
        r"\b(?:the person is|identified person is)\s+(.+?)"
        r"(?:,|\bbut\b|\band\b|\bwith\b|\.)",
        text, re.IGNORECASE)
    return " ".join(match.group(1).strip(' \"“”').split()) if match else ""


def _has_verified_person(lead_id: int) -> bool:
    """Return whether Grant stores a real name and email for this exact lead."""
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT 1 FROM contacts
               WHERE lead_id=? AND contact_status='verified'
                 AND TRIM(COALESCE(name,''))!=''
                 AND TRIM(COALESCE(email,''))!='' LIMIT 1""",
            (lead_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _organization_preview_failure(tool_text: str) -> str:
    """Translate a safe preview failure into brief, nontechnical Slack language."""
    if "matching record" in tool_text:
        link = next(iter(re.findall(r"https?://[^\s,]+", tool_text)), "")
        suffix = f" Here is the possible match: {link}" if link else ""
        return ("Salesforce may already have this organization, so I did not create a "
                f"duplicate.{suffix}")
    if "verified current funding source" in tool_text:
        return ("I can’t safely create this Lead because its verified funding source is "
                "missing. Nothing was changed.")
    return ("I couldn’t safely prepare that Salesforce Lead because the duplicate check "
            "did not complete. Nothing was changed.")


def _is_pending_org_preview_question(text: str) -> bool:
    """Recognize a plain-English question about the exact pending org preview."""
    normalized = " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())
    preview = "preview" in normalized or "if confirmed" in normalized
    asks_email = "email" in normalized and any(
        phrase in normalized for phrase in ("require", "need", "add", "without"))
    asks_changes = any(
        phrase in normalized for phrase in ("what will change", "what changes", "exactly what"))
    return preview and (asks_email or asks_changes)


def _pending_org_enrichment_reply(
        text: str, workspace: str, channel: str, thread_ts: str,
        requester: str) -> str | None:
    """Explain one requester-bound pending org preview from its immutable payload."""
    if not _is_pending_org_preview_question(text):
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT payload_json,payload_hash,expires_at FROM crm_actions
               WHERE action_type='enrich_existing_lead' AND state='ready'
                 AND workspace=? AND channel=? AND thread_ts=? AND requested_by=?
               ORDER BY created_at DESC LIMIT 1""",
            (workspace, channel, thread_ts, requester),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload_json = str(row["payload_json"])
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(row["payload_hash"]):
        return None
    try:
        if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    try:
        payload: dict[str, Any] = json.loads(payload_json)
        delta = dict(payload["delta"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    labels = {
        "Website": "Website", "Phone": "Phone", "Street": "Street",
        "City": "City", "State": "State", "PostalCode": "Postal code",
        "Country": "Country", "Industry": "Industry",
        "Number_of_Students__c": "Student enrollment",
        "Description": "Research notes",
    }
    fields = [labels[key] for key in delta if key in labels]
    if not fields:
        return None
    bullets = "\n".join(f"• {field}" for field in fields)
    return (
        "No. This preview does not require or add an email.\n\n"
        "If you confirm it, Grant will update only these fields on the one Lead shown:\n\n"
        f"{bullets}\n\n"
        "It will also add a visible research Note and a completed administrative "
        "Activity. It will not create another Lead, person, Campaign, or Opportunity."
    )
