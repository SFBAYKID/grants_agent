"""Bounded pre-window refresh for rich contact and Salesforce-call evidence.

The worker never posts or writes Salesforce. Contact discovery is the only possibly
paid path and is durably marked before HTTP by ``contact_evidence.refresh``. Dry-run
does no network I/O and writes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ..enrich import salesforce_activity
from . import contact_evidence, paid_calls, preparation

ActivityLookup = Callable[[str, frozenset[str]], salesforce_activity.ActivityEvidence]


@dataclass(frozen=True)
class PreparationSummary:
    """Truthful bounded-worker counts without contact or CRM PII."""

    candidates: int
    contact_fresh: int
    contact_refreshed: int
    activity_checked: int
    indeterminate: int
    errors: int
    writes: int


def run(
    conn: sqlite3.Connection,
    audience: str,
    *,
    limit: int = 25,
    dry_run: bool = True,
    retry_indeterminate: bool = False,
    contact_finder: contact_evidence.Finder = contact_evidence._default_finder,
    activity_lookup: ActivityLookup = salesforce_activity.lookup_recent_completed_call,
    now: datetime | None = None,
) -> PreparationSummary:
    """Refresh a bounded quality-ordered batch or report the planned batch only."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lead_ids = preparation.candidate_lead_ids(conn, audience, limit)
    if dry_run:
        return PreparationSummary(len(lead_ids), 0, 0, 0, 0, 0, 0)
    counts = {
        "contact_fresh": 0,
        "contact_refreshed": 0,
        "activity_checked": 0,
        "indeterminate": 0,
        "errors": 0,
        "writes": 0,
    }
    for lead_id in lead_ids:
        try:
            status = contact_evidence.refresh(
                conn,
                lead_id,
                finder_fn=contact_finder,
                retry_indeterminate=retry_indeterminate,
                now=at,
            )
            if status == "fresh":
                counts["contact_fresh"] += 1
            else:
                counts["contact_refreshed"] += 1
                counts["writes"] += 1
        except paid_calls.CompletedPaidCall:
            counts["contact_fresh"] += 1
        except paid_calls.IndeterminatePaidCall:
            counts["indeterminate"] += 1
        except Exception:  # noqa: BLE001 - one candidate cannot abort the bounded batch
            counts["errors"] += 1
        account_id, person_ids = preparation.exact_crm_bindings(conn, lead_id)
        if not account_id or not person_ids:
            continue
        try:
            evidence = activity_lookup(account_id, person_ids)
            salesforce_activity.persist(conn, lead_id, evidence)
            counts["activity_checked"] += 1
            counts["writes"] += 1
        except Exception:  # noqa: BLE001 - honest per-candidate failure count
            counts["errors"] += 1
    return PreparationSummary(
        candidates=len(lead_ids),
        contact_fresh=counts["contact_fresh"],
        contact_refreshed=counts["contact_refreshed"],
        activity_checked=counts["activity_checked"],
        indeterminate=counts["indeterminate"],
        errors=counts["errors"],
        writes=counts["writes"],
    )
