"""Fill in the organization details a rep would otherwise have to research by hand.

WHY THIS EXISTS, WITH THE NUMBER THAT PROVES IT. Chase opened a Lead in the
California campaign and found an empty address. The Salesforce payload was fixed to
carry Street/City/PostalCode/Website/students/Industry — and then production was
measured, which is the only reason we know that fix was nearly inert: **22 of 10,715
leads have a street address (0.21%)**, 16 of 286 gold (5.6%). The mapping was
correct and there was almost nothing to map.

The cause is that `enrich_org_profile` only ever ran one lead at a time — the daily
rich-card prepare worker (about one lead a day) and the `find_contact` tool (one
lead, one rep, one click). Nothing ever swept the corpus, so the columns stayed
empty and every Lead written from them was thin.

THIS SPENDS MONEY, so it is bounded and dry-run by default. Each lead is a live
scrape. Gold first is not a convenience: gold is where leads are actually worked, and
at 286 rows the whole tier is affordable in one pass, while all 10,715 is not.

FAILURES ARE PER-LEAD. One unreachable site must not end the sweep — that is the
same wedge that let a single malformed reminder silence the whole reminder queue.
Nothing is invented: a site that cannot be read records `unreachable` and is
retryable, exactly as the single-lead path already behaves.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import canonical_entity_key
from .enrich.organization_profile import enrich_org_profile

# One pass is deliberately capped. An unbounded sweep over 10,715 leads is a bill
# nobody approved, and the tier that matters is 286 rows.
DEFAULT_LIMIT = 50

# How long a failed organization lookup rests before it is worth paying for again.
# Two weeks: long enough that a batch cannot re-buy this morning's failures, short
# enough that a site which was merely down comes back within the same working month.
RETRY_AFTER_DAYS = 14


def _canonical_key_sql(entity: object, state: object) -> str:
    """SQLite adapter for :func:`db_common.canonical_entity_key`.

    Registered per call rather than imported into SQL text because SQLite has no
    regex; routing through the real function is what stops the grouping key from
    drifting away from the stored `canonical_entity_key` column a second time.
    """
    return canonical_entity_key(str(entity or ""), str(state or ""))


@dataclass(frozen=True)
class BackfillOutcome:
    """What one sweep actually did — counted, never estimated."""

    considered: int
    enriched: int
    unreachable: int
    failed: int

    def summary(self) -> str:
        """One honest line for the operator."""
        return (
            f"considered {self.considered}, filled {self.enriched}, "
            f"unreachable {self.unreachable}, errored {self.failed}"
        )


def candidates(
    conn: sqlite3.Connection,
    *,
    grade: str = "gold",
    limit: int = DEFAULT_LIMIT,
    retry_after_days: int = RETRY_AFTER_DAYS,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    """Leads with no usable organization profile yet, best-scoring first.

    `org_profile_status='found'` short-circuits inside `enrich_org_profile`, so those
    are excluded here rather than paid for and discarded. An `unreachable` or
    `not_found` lead IS still included — that outcome is explicitly retryable — but
    only once `retry_after_days` have passed since the last attempt.

    THE COOLDOWN IS THE FIX FOR A MEASURED WASTE, not a new policy. Retryable was
    read as retryable IMMEDIATELY, so a lead whose site returned nothing became a
    candidate again on the very next run: on 2026-08-13 production held 21 `not_found`
    plus 2 `unreachable` gold rows that would have eaten ~108 of a following batch's
    ~352 Firecrawl calls re-fetching pages that had just failed. A NULL
    `org_profile_checked_at` (every pre-migration-47 row) is eligible at once, because
    "never measured" is not "just tried".

    ONE ROW PER ORGANIZATION. The sweep pays per lead, and gold alone holds ~30
    duplicated entity names. Grouping on the canonical key means each organization is
    fetched once; the profile is stored per lead, so the duplicates are picked up on a
    later pass rather than paid for twice in this one.

    THE KEY IS RECOMPUTED, NEVER READ FROM THE COLUMN. The previous
    `COALESCE(NULLIF(canonical_entity_key,''), entity_name)` fell back to the RAW
    name, which cannot equal a stored key — the real key is lower-cased, punctuation-
    folded and state-suffixed. So a NULL-key row (`'MODESTO CITY SCHOOLS'`) and its
    populated twin (`'modesto city schools|CA'`) landed in DIFFERENT groups and the
    organization was scraped twice anyway. Modesto and Mt. Morris are the very
    examples this docstring used to cite as fixed, and both recurred live on
    2026-08-13. Registering `db.canonical_entity_key` as a SQL function makes the
    grouping key the same one function everywhere, so it cannot drift again — SQLite
    has no regex, so no inline SQL expression could have matched it.

    ORDERING IS BY AWARD AMOUNT, and the honest caveat is that `amount` is NULL on
    most gold rows, so in practice this degrades to id order. `lead_score` would be
    the right key and cannot be used — it is a computed function in `scoring.py`, not
    a column, and ordering by it in SQL fails outright. Said plainly rather than left
    as a claim the data does not support.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=max(0, retry_after_days))).isoformat(
        timespec="seconds"
    )
    conn.create_function("grant_canonical_entity_key", 2, _canonical_key_sql)
    return list(
        conn.execute(
            """SELECT MIN(id) AS id, entity_name, state, amount
                 FROM leads
                WHERE lead_grade = ?
                  AND COALESCE(org_profile_status,'') <> 'found'
                  AND COALESCE(entity_name,'') <> ''
                  AND (org_profile_checked_at IS NULL
                       OR org_profile_checked_at < ?)
                GROUP BY grant_canonical_entity_key(entity_name, COALESCE(state,''))
                ORDER BY COALESCE(MAX(amount),0) DESC, MIN(id)
                LIMIT ?""",
            (grade, cutoff, max(1, limit)),
        )
    )


def run(
    conn: sqlite3.Connection,
    *,
    grade: str = "gold",
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = True,
) -> BackfillOutcome:
    """Sweep one bounded batch of leads, filling in what their own site publishes."""
    rows = candidates(conn, grade=grade, limit=limit)
    if dry_run:
        for row in rows:
            print(f"  would enrich #{row['id']} {row['entity_name']} ({row['state']})")
        return BackfillOutcome(len(rows), 0, 0, 0)

    enriched = unreachable = failed = 0
    for row in rows:
        lead_id = int(row["id"])
        try:
            profile = enrich_org_profile(conn, lead_id)
        except Exception as exc:  # noqa: BLE001 — one bad site must not end the sweep
            failed += 1
            print(f"  #{lead_id} {row['entity_name']}: {type(exc).__name__}")
            continue
        # `street` is the field the whole exercise is about; a profile that found a
        # website but no address is progress, so both are counted as filled.
        if profile.street or profile.website or profile.phone:
            enriched += 1
            print(f"  #{lead_id} {row['entity_name']}: filled")
        else:
            unreachable += 1
            print(f"  #{lead_id} {row['entity_name']}: nothing published")
    return BackfillOutcome(len(rows), enriched, unreachable, failed)
