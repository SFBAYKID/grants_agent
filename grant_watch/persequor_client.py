"""Grant -> Persequor outreach client, per the agreed outreach-request.v1 contract
(docs/workflow_design.md §4; Persequor's integration response 2026-07-13).

Wire facts: both apps live on the same droplet, so the endpoint is localhost —
PERSEQUOR_API_URL (default http://127.0.0.1:8002), shared secret in the
X-Persequor-Key header (PERSEQUOR_API_KEY in both projects' .env, never committed).

Idempotency (architectural-critic C2): the request_id is a UUID minted ONCE and
persisted on the outreach row BEFORE the first POST; every retry reuses it, so
Persequor's unique index can never mint a second draft card for the same ask.

TEST MODE (Chase, 2026-07-13): when OUTREACH_TEST_EMAIL is set, contact_email in the
brief is the test address and the REAL discovered contact rides in rep_notes for
verification. Clear the env var to go live.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import requests

from . import db

REPS_PATH = Path(__file__).resolve().parent.parent / "config" / "reps.json"
DEFAULT_API = "http://127.0.0.1:8002"
TIMEOUT_S = 15


def rep_email_for(slack_id: str) -> str | None:
    """send_as is ALWAYS derived from the roster map — never free-form."""
    data = json.loads(REPS_PATH.read_text())
    for rep in data["reps"]:
        if rep["slack_id"] == slack_id:
            return rep["email"]
    return None


def build_brief(row: sqlite3.Row, contact: sqlite3.Row | None,
                requested_by_slack: str, send_as: str,
                rep_notes: str | None = None,
                slack_channel: str | None = None,
                slack_thread_ts: str | None = None) -> dict[str, Any] | None:
    """outreach-request.v1 payload. Returns None when there is no verified contact
    AND no test override (Persequor would just bounce needs_contact — we gate here,
    per the design)."""
    test_email = os.environ.get("OUTREACH_TEST_EMAIL", "").strip()
    real_email = contact["email"] if contact is not None else None
    real_name = contact["name"] if contact is not None else None
    real_title = contact["title"] if contact is not None else None

    if not real_email and not test_email:
        return None
    notes = rep_notes or ""
    if test_email:
        # Test mode: address the test mailbox; keep the truth visible in the notes.
        real_desc = (f"real discovered contact: {real_name} ({real_title}) "
                     f"<{real_email}>" if real_email else "no verified contact yet")
        notes = (f"[TEST MODE — send to {test_email} only; {real_desc}] " + notes).strip()

    return {
        "schema": "outreach-request.v1",
        "request_id": f"grant-{row['id']}-{uuid.uuid4().hex[:12]}",
        "entity": row["entity_name"],
        "entity_type": row["entity_type"] or "school_district",
        "state": row["state"] or "",
        "program": row["program"] or "",
        "amount_usd": int(round(row["amount"])) if row["amount"] else None,
        "window_start": row["funds_start"] or None,
        "window_end": row["funds_end"] or None,
        "source_url": row["detail_url"] or None,
        "requested_by_slack": requested_by_slack,
        "send_as": send_as,
        "contact_name": (real_name if not test_email else real_name) or None,
        "contact_email": test_email or real_email,
        "contact_title": real_title or None,
        "angle": "fresh award, camera/access-control eligible, open spend window",
        "rep_notes": notes or None,
        "expires_at": row["funds_end"] or None,  # critic M4: no stale-facts sends
        # WHERE the conversation lives — so Persequor renders its approval card as a
        # reply IN Grant's lead thread, not loose in the channel (Chase, 2026-07-14).
        "slack_channel": slack_channel,
        "slack_thread_ts": slack_thread_ts,
    }


def submit_brief(conn: sqlite3.Connection, lead_id: int,
                 brief: dict[str, Any]) -> tuple[str, str]:
    """Persist first (idempotency), then POST. Returns (state, human_message).

    state: 'submitted' | 'unreachable' | 'rejected' — always truthful; an
    unreachable endpoint is queued locally and said out loud, never silently lost.
    """
    # Persist the request id + draft-brief BEFORE any network I/O.
    cur = conn.execute(
        "INSERT INTO outreach (lead_id, channel, draft) VALUES (?, 'persequor', ?)",
        (lead_id, json.dumps(brief)))
    conn.commit()
    outreach_id = int(cur.lastrowid)

    url = os.environ.get("PERSEQUOR_API_URL", DEFAULT_API).rstrip("/")
    key = os.environ.get("PERSEQUOR_API_KEY", "")
    try:
        resp = requests.post(f"{url}/api/v1/outreach-request", json=brief,
                             headers={"X-Persequor-Key": key}, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        return "unreachable", (f"Persequor isn't reachable right now "
                               f"({type(exc).__name__}) — I've queued the request "
                               f"(#{outreach_id}) and will retry.")
    if resp.status_code in (200, 201, 202):
        return "submitted", ("Sent to Persequor — the draft will show up in your DM "
                             "for approval before anything goes out.")
    if resp.status_code == 404:
        return "unreachable", ("Persequor's intake endpoint isn't live yet — the "
                               "request is queued locally until it ships.")
    return "rejected", (f"Persequor declined the request "
                        f"(HTTP {resp.status_code}: {resp.text[:120]}).")
