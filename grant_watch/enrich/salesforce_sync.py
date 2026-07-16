"""Read-only Salesforce snapshot worker for proactive lead prioritization.

Why: an existing open Opportunity is the highest-value context Chase described, but
the drip worker must not make dozens of live CRM calls while choosing one message.
This bounded worker queries Salesforce through the GET-only reader and persists record
links/status locally. It never imports or uses the Campaign writer gateway.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import scoring
from . import salesforce

STALE_HOURS = 24


@dataclass(frozen=True)
class SyncSummary:
    """Truthful counts from one bounded Salesforce reader pass."""

    checked: int
    found: int
    no_match: int
    ambiguous: int
    partial: int
    unavailable: int
    writes: int


def _now() -> str:
    """Return one UTC timestamp for snapshot provenance."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Return the highest-base-value active leads whose CRM snapshot is stale/missing."""
    rows = list(
        conn.execute(
            """SELECT l.*,e.occurred_on AS event_date
           FROM leads l
           LEFT JOIN funding_events e ON e.id=l.current_event_id
           LEFT JOIN salesforce_lookup_state s ON s.lead_id=l.id
           WHERE COALESCE(l.status,'new') NOT IN ('dead','contacted')
             AND (s.checked_at IS NULL OR datetime(s.checked_at) < datetime('now', ?))
           LIMIT 500""",
            (f"-{STALE_HOURS} hours",),
        )
    )
    rows.sort(
        key=lambda row: scoring.lead_score(
            str(row["program"] or ""), row["amount"], str(row["event_date"] or "")
        ),
        reverse=True,
    )
    return rows[: max(1, min(limit, 100))]


def _persist(
    conn: sqlite3.Connection, lead_id: int, result: salesforce.SFResult
) -> None:
    """Replace a completed lookup snapshot; retain old matches during an outage."""
    checked_at = _now()
    with conn:
        conn.execute(
            """INSERT INTO salesforce_lookup_state(lead_id,status,error,checked_at)
               VALUES (?,?,?,?)
               ON CONFLICT(lead_id) DO UPDATE SET status=excluded.status,
                 error=excluded.error,checked_at=excluded.checked_at""",
            (lead_id, result.status.value, result.error or None, checked_at),
        )
        if result.status is salesforce.SFResultStatus.UNAVAILABLE:
            return
        conn.execute("DELETE FROM salesforce_matches WHERE lead_id=?", (lead_id,))
        conn.executemany(
            """INSERT INTO salesforce_matches
                 (lead_id,sobject,record_id,name,company,owner,link,confidence,
                  account_id,stage,is_closed,checked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    lead_id,
                    match.sobject,
                    match.record_id,
                    match.name,
                    match.company or None,
                    match.owner or None,
                    match.link,
                    match.confidence,
                    match.account_id or None,
                    match.stage or None,
                    int(match.is_closed) if match.is_closed is not None else None,
                    checked_at,
                )
                for match in result.matches
            ],
        )


def sync(
    conn: sqlite3.Connection, limit: int = 25, dry_run: bool = False
) -> SyncSummary:
    """Query a bounded lead batch and optionally persist read-only CRM snapshots."""
    counts = {status.value: 0 for status in salesforce.SFResultStatus}
    candidates = _candidates(conn, limit)
    writes = 0
    for row in candidates:
        result = salesforce.lookup(
            str(row["entity_name"]), state=str(row["state"] or "")
        )
        counts[result.status.value] += 1
        if not dry_run:
            _persist(conn, int(row["id"]), result)
            writes += 1
    return SyncSummary(
        checked=len(candidates),
        found=counts[salesforce.SFResultStatus.FOUND.value],
        no_match=counts[salesforce.SFResultStatus.NO_MATCH.value],
        ambiguous=counts[salesforce.SFResultStatus.AMBIGUOUS.value],
        partial=counts[salesforce.SFResultStatus.PARTIAL.value],
        unavailable=counts[salesforce.SFResultStatus.UNAVAILABLE.value],
        writes=writes,
    )
