"""Grant -> Persequor outreach client, per the agreed outreach-request.v1 contract
(docs/workflow_design.md §4; Persequor's integration response 2026-07-13).

Wire facts: both apps live on the same droplet, so the endpoint is localhost —
PERSEQUOR_API_URL (default http://127.0.0.1:8002), shared secret in the
X-Persequor-Key header (PERSEQUOR_API_KEY in both projects' .env, never committed).

Idempotency (architectural-critic C2): request identity includes the triggering Slack
message. Redelivery/retry of that message reuses one key; a later explicit "draft
again" message gets a new key and therefore a fresh human-reviewed Gmail draft.

TEST MODE (Chase, 2026-07-13): when OUTREACH_TEST_EMAIL is set, contact_email in the
brief is the test address and the REAL discovered contact rides in rep_notes for
verification. Clear the env var to go live.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import requests

from .presentation import display_entity_name, strip_leading_honorifics
from .record_semantics import semantics_for

REPS_PATH = Path(__file__).resolve().parent.parent / "config" / "reps.json"
DEFAULT_API = "http://127.0.0.1:8002"
TIMEOUT_S = 15
DEFAULT_MAX_ATTEMPTS = 5


class OutreachBrief(TypedDict):
    """The complete typed ``outreach-request.v1`` payload sent to Persequor."""

    schema: str
    request_id: str
    entity: str
    entity_type: str
    state: str
    program: str
    amount_usd: int | None
    window_start: str | None
    window_end: str | None
    source_url: str | None
    requested_by_slack: str
    send_as: str
    contact_name: str | None
    contact_email: str | None
    contact_title: str | None
    angle: str
    rep_notes: str | None
    expires_at: str | None
    slack_channel: str | None
    slack_thread_ts: str | None


@dataclass(frozen=True)
class RetrySummary:
    """Counts from one bounded retry-worker pass."""

    due: int
    submitted: int
    queued: int
    rejected: int


def _now() -> str:
    """Return one UTC timestamp in the database's standard ISO representation."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _angle(row: sqlite3.Row) -> str:
    """Describe only the funding state the canonical lead projection proves.

    Delegates to `record_semantics`, which derives meaning from `current_event_type`
    and never from `lead_grade`. Grade is a priority signal; it cannot say what
    happened. See that module's header for the incident this prevents.
    """
    return semantics_for(row).angle


def rep_email_for(slack_id: str) -> str | None:
    """send_as is ALWAYS derived from the roster map — never free-form."""
    data = json.loads(REPS_PATH.read_text())
    for rep in data["reps"]:
        if rep["slack_id"] == slack_id:
            return rep["email"]
    return None


def request_id_for(
    row: sqlite3.Row,
    requested_by_slack: str,
    slack_channel: str,
    slack_thread_ts: str,
    request_token: str,
) -> str:
    """Return one stable key per explicit Slack request, not per whole thread."""
    event_id = str(row["current_event_id"] or "projection")
    context = "|".join(
        (
            str(row["id"]),
            event_id,
            requested_by_slack,
            slack_channel,
            slack_thread_ts,
            request_token,
        )
    )
    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()[:20]
    return f"grant-{row['id']}-{event_id}-{digest}"


def build_brief(
    row: sqlite3.Row,
    contact: sqlite3.Row | None,
    requested_by_slack: str,
    send_as: str,
    rep_notes: str | None = None,
    slack_channel: str | None = None,
    slack_thread_ts: str | None = None,
    request_id: str | None = None,
) -> OutreachBrief | None:
    """outreach-request.v1 payload. Returns None when there is no verified contact
    AND no test override (Persequor would just bounce needs_contact — we gate here,
    per the design)."""
    test_email = os.environ.get("OUTREACH_TEST_EMAIL", "").strip()
    real_email = contact["email"] if contact is not None else None
    # Strip an honorific so Persequor's greeting reads "Hi Joel," not "Hi Mr.,".
    real_name = (
        strip_leading_honorifics(contact["name"]) or None
        if contact is not None
        else None
    )
    real_title = contact["title"] if contact is not None else None

    if not real_email and not test_email:
        return None
    notes = rep_notes or ""
    if test_email:
        # Test mode: address the test mailbox; keep the truth visible in the notes.
        real_desc = (
            f"real discovered contact: {real_name} ({real_title}) <{real_email}>"
            if real_email
            else "no verified contact yet"
        )
        notes = (
            f"[TEST MODE — send to {test_email} only; {real_desc}] " + notes
        ).strip()

    _meaning = semantics_for(row)
    return {
        "schema": "outreach-request.v1",
        "request_id": request_id or f"grant-{row['id']}-{uuid.uuid4().hex[:12]}",
        "entity": display_entity_name(row["entity_name"]),
        "entity_type": row["entity_type"] or "school_district",
        "state": row["state"] or "",
        "program": row["program"] or "",
        # Gated on the record kind, NOT merely on presence. Persequor is an LLM
        # drafting agent: give it `program="SVPP"` and `amount_usd=487657` and it will
        # write "your $487,657 SVPP award", however carefully `angle` is hedged. On a
        # record whose kind cannot establish what the amount represents, sending the
        # figure IS the award claim. Withheld rather than relabelled, because
        # outreach-request.v1 is a pinned external schema and inventing a key here
        # could 422 every brief if Persequor forbids unknown fields.
        "amount_usd": (
            int(round(row["amount"]))
            if row["amount"] and _meaning.asserts_amount
            else None
        ),
        # The window fields ship ONLY when the record kind gives them a stateable
        # meaning; `angle` already states which meaning that is, in prose the schema
        # supports. No new key is introduced: `outreach-request.v1` is pinned and
        # agreed with Persequor, and an unknown field would 422 every brief if their
        # endpoint forbids extras. Changing the shape needs their agreement first.
        "window_start": (row["funds_start"] or None) if _meaning.asserts_dates else None,
        "window_end": (row["funds_end"] or None) if _meaning.asserts_dates else None,
        "source_url": row["current_event_source_url"] or row["detail_url"] or None,
        "requested_by_slack": requested_by_slack,
        "send_as": send_as,
        "contact_name": (real_name if not test_email else real_name) or None,
        "contact_email": test_email or real_email,
        "contact_title": real_title or None,
        "angle": _angle(row),
        "rep_notes": notes or None,
        "expires_at": (row["funds_end"] or None) if _meaning.asserts_dates else None,
        # WHERE the conversation lives — so Persequor renders its approval card as a
        # reply IN Grant's lead thread, not loose in the channel (Chase, 2026-07-14).
        "slack_channel": slack_channel,
        "slack_thread_ts": slack_thread_ts,
    }


def submit_brief(
    conn: sqlite3.Connection, lead_id: int, brief: OutreachBrief
) -> tuple[str, str]:
    """Persist first (idempotency), then POST. Returns (state, human_message).

    state: 'submitted' | 'unreachable' | 'rejected' — always truthful; an
    unreachable endpoint is queued locally and said out loud, never silently lost.
    """
    # Persist the request id + exact draft BEFORE network I/O. A repeated Slack event or
    # worker retry reuses the row and Persequor's idempotency key.
    request_id = brief["request_id"]
    now = _now()
    with conn:
        inserted = conn.execute(
            """INSERT OR IGNORE INTO outreach
                 (lead_id,channel,draft,request_id,status,attempts,created_at)
               VALUES (?, 'persequor', ?, ?, 'queued', 0, ?)""",
            (lead_id, json.dumps(brief, sort_keys=True), request_id, now),
        )
    saved = conn.execute(
        "SELECT * FROM outreach WHERE request_id=?", (request_id,)
    ).fetchone()
    assert saved is not None
    if saved["status"] == "submitted":
        return "submitted", "This request was already accepted by Persequor."
    if saved["status"] == "rejected":
        return (
            "rejected",
            "Persequor previously rejected this request; it was not retried.",
        )
    if inserted.rowcount == 0:
        if saved["status"] in {"queued", "sending"}:
            return "unreachable", (
                "This exact request is already queued or being sent to "
                "Persequor; Grant did not create another copy."
            )
        if saved["status"] == "failed":
            return "rejected", (
                "This request reached its retry limit; Grant did not "
                "create another copy."
            )
    return _attempt_saved(conn, saved)


def _attempt_saved(conn: sqlite3.Connection, outreach: sqlite3.Row) -> tuple[str, str]:
    """POST one persisted request and durably record its truthful delivery state."""
    outreach_id = int(outreach["id"])
    brief: OutreachBrief = json.loads(str(outreach["draft"]))
    attempt = int(outreach["attempts"] or 0) + 1
    with conn:
        claimed = conn.execute(
            """UPDATE outreach SET status='sending', attempts=?, last_error=NULL,
                      next_attempt_at=NULL WHERE id=? AND status='queued'""",
            (attempt, outreach_id),
        )
    if claimed.rowcount != 1:
        return "unreachable", (
            "This request is already being processed; Grant did not send another copy."
        )

    url = os.environ.get("PERSEQUOR_API_URL", DEFAULT_API).rstrip("/")
    key = os.environ.get("PERSEQUOR_API_KEY", "")
    try:
        resp = requests.post(
            f"{url}/api/v1/outreach-request",
            json=brief,
            headers={"X-Persequor-Key": key},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as exc:
        _queue_retry(conn, outreach_id, attempt, type(exc).__name__)
        return "unreachable", (
            f"Persequor isn't reachable right now "
            f"({type(exc).__name__}) — I've queued the request "
            f"(#{outreach_id}) and will retry."
        )
    if resp.status_code in (200, 201, 202):
        with conn:
            conn.execute(
                """UPDATE outreach SET status='submitted', submitted_at=?,
                          next_attempt_at=NULL, last_error=NULL WHERE id=?""",
                (_now(), outreach_id),
            )
        return "submitted", (
            "Persequor accepted the request and will prepare a new "
            "Gmail draft for your review. Nothing was sent."
        )
    if resp.status_code == 404 or resp.status_code >= 500:
        _queue_retry(conn, outreach_id, attempt, f"HTTP {resp.status_code}")
        return "unreachable", (
            "Persequor's intake is unavailable right now — the "
            f"request is queued locally (#{outreach_id}) for retry."
        )
    error = f"HTTP {resp.status_code}"
    with conn:
        conn.execute(
            """UPDATE outreach SET status='rejected', last_error=?,
                      next_attempt_at=NULL WHERE id=?""",
            (error, outreach_id),
        )
    return "rejected", f"Persequor declined the request (HTTP {resp.status_code})."


def _queue_retry(
    conn: sqlite3.Connection, outreach_id: int, attempt: int, error: str
) -> None:
    """Schedule a bounded exponential retry while retaining the same request id."""
    delay_minutes = min(60, 5 * (2 ** max(0, attempt - 1)))
    next_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat(
        timespec="seconds"
    )
    status = "queued" if attempt < DEFAULT_MAX_ATTEMPTS else "failed"
    with conn:
        conn.execute(
            """UPDATE outreach SET status=?,last_error=?,next_attempt_at=? WHERE id=?""",
            (status, error, next_at if status == "queued" else None, outreach_id),
        )


def retry_pending(
    conn: sqlite3.Connection, dry_run: bool = False, limit: int = 20
) -> RetrySummary:
    """Retry due approved handoffs once each; dry-run performs no writes or requests."""
    rows = list(
        conn.execute(
            """SELECT * FROM outreach
           WHERE channel='persequor' AND status='queued'
             AND attempts < ?
             AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
           ORDER BY COALESCE(next_attempt_at, created_at), id LIMIT ?""",
            (DEFAULT_MAX_ATTEMPTS, _now(), max(1, min(limit, 100))),
        )
    )
    if dry_run:
        return RetrySummary(len(rows), 0, len(rows), 0)
    submitted = queued = rejected = 0
    for row in rows:
        try:
            json.loads(str(row["draft"]))
        except (json.JSONDecodeError, TypeError):
            with conn:
                conn.execute(
                    """UPDATE outreach SET status='rejected',last_error=?,
                              next_attempt_at=NULL WHERE id=?""",
                    ("invalid persisted outreach payload", int(row["id"])),
                )
            rejected += 1
            continue
        state, _message = _attempt_saved(conn, row)
        submitted += int(state == "submitted")
        queued += int(state == "unreachable")
        rejected += int(state == "rejected")
    return RetrySummary(len(rows), submitted, queued, rejected)
