"""Bounded pre-window refresh for rich contact and Salesforce-call evidence.

The worker never posts or writes Salesforce. Contact discovery is the only possibly
paid path and is durably marked before HTTP by ``contact_evidence.refresh``. Dry-run
does no network I/O and writes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..enrich import organization_profile, salesforce_activity, salesforce_sync
from . import contact_evidence, paid_calls, policy, preparation

ActivityLookup = Callable[[str, frozenset[str]], salesforce_activity.ActivityEvidence]
# Returns the enriched profile object, which this worker deliberately ignores -- the
# only thing that matters here is the persisted `leads.org_website`, which the policy
# reads later. Typed as `object` because the worker asserts nothing about the shape.
WebsiteFinder = Callable[[sqlite3.Connection, int], object]
# Refreshes one lead's read-only CRM snapshot; returns its status string.
CrmRefresh = Callable[[sqlite3.Connection, int], str]


def _crm_is_stale(conn: sqlite3.Connection, lead_id: int, now: datetime) -> bool:
    """Whether this lead's CRM snapshot is missing, unparseable, future, or expired.

    The policy rejects any candidate whose CRM state is older than CRM_FRESH_HOURS, and
    NOTHING scheduled writes `salesforce_lookup_state` -- the table stood at 0 rows in
    production while the rich card was enabled, which alone made every candidate
    CRM_UNSAFE and the card unpostable. `salesforce_sync.sync` cannot fill the gap
    because it ranks globally and caps at 100; see salesforce_sync.refresh_lead.
    """
    row = conn.execute(
        "SELECT checked_at FROM salesforce_lookup_state WHERE lead_id=?", (lead_id,)
    ).fetchone()
    if row is None:
        return True
    try:
        checked = datetime.fromisoformat(str(row["checked_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return not (now - timedelta(hours=policy.CRM_FRESH_HOURS) <= checked <= now)


def _needs_website(conn: sqlite3.Connection, lead_id: int) -> bool:
    """Whether this lead has never had an organization-profile attempt recorded.

    `enrich_org_profile` short-circuits only on a prior ``found``; a ``not_found`` or
    ``unreachable`` lead would otherwise be re-scraped (Firecrawl + Anthropic) on every
    single weekday run forever. One attempt per lead is the honest bound -- a retry
    policy is a separate, deliberate decision, not an accident of scheduling.
    """
    row = conn.execute(
        "SELECT org_profile_status FROM leads WHERE id=?", (lead_id,)
    ).fetchone()
    return row is not None and not str(row["org_profile_status"] or "")


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
    website_checked: int = 0
    crm_checked: int = 0


def run(
    conn: sqlite3.Connection,
    audience: str,
    *,
    limit: int = 25,
    dry_run: bool = True,
    retry_indeterminate: bool = False,
    contact_finder: contact_evidence.Finder = contact_evidence._default_finder,
    activity_lookup: ActivityLookup = salesforce_activity.lookup_recent_completed_call,
    website_finder: WebsiteFinder = organization_profile.enrich_org_profile,
    crm_refresh: CrmRefresh = salesforce_sync.refresh_lead,
    now: datetime | None = None,
) -> PreparationSummary:
    """Refresh a bounded quality-ordered batch or report the planned batch only."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # Target only leads whose remaining blockers preparation can close. Ordering by raw
    # lead_score paid to enrich leads that the policy must reject regardless of what was
    # learned (see preparation.REMEDIABLE_REASONS).
    lead_ids = preparation.preparable_lead_ids(conn, audience, limit=limit, now=at)
    if dry_run:
        return PreparationSummary(len(lead_ids), 0, 0, 0, 0, 0, 0)
    counts = {
        "contact_fresh": 0,
        "contact_refreshed": 0,
        "activity_checked": 0,
        "indeterminate": 0,
        "errors": 0,
        "writes": 0,
        "website_checked": 0,
        "crm_checked": 0,
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
            # A completed paid attempt can mean verified, removed, or not_found.
            # Preparation readiness comes from contact_evidence, never call status.
            pass
        except paid_calls.IndeterminatePaidCall:
            counts["indeterminate"] += 1
        except Exception:  # noqa: BLE001 - one candidate cannot abort the bounded batch
            counts["errors"] += 1
        # Website discovery runs AFTER the contact refresh on purpose: `_resolve_site`
        # prefers a verified contact's own domain, so a contact found moments ago turns
        # what would be a paid Firecrawl SEARCH into a direct scrape of a known host.
        if _needs_website(conn, lead_id):
            try:
                website_finder(conn, lead_id)
                counts["website_checked"] += 1
                counts["writes"] += 1
            except Exception:  # noqa: BLE001 - a dead site cannot abort the batch
                counts["errors"] += 1
        # CRM state must exist and be fresh before `exact_crm_bindings` can return
        # anything, and before the policy will accept the candidate at all. This is the
        # step that had no scheduled writer, which is why salesforce_lookup_state sat
        # empty and no rich card could ever post.
        if _crm_is_stale(conn, lead_id, at):
            try:
                crm_refresh(conn, lead_id)
                counts["crm_checked"] += 1
                counts["writes"] += 1
            except Exception:  # noqa: BLE001 - a CRM outage cannot abort the batch
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
        website_checked=counts["website_checked"],
        crm_checked=counts["crm_checked"],
    )
