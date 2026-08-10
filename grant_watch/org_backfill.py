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

from .enrich.organization_profile import enrich_org_profile

# One pass is deliberately capped. An unbounded sweep over 10,715 leads is a bill
# nobody approved, and the tier that matters is 286 rows.
DEFAULT_LIMIT = 50


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
    conn: sqlite3.Connection, *, grade: str = "gold", limit: int = DEFAULT_LIMIT
) -> list[sqlite3.Row]:
    """Leads with no usable organization profile yet, best-scoring first.

    `org_profile_status='found'` short-circuits inside `enrich_org_profile`, so those
    are excluded here rather than paid for and discarded. An `unreachable` lead IS
    included: that outcome is explicitly retryable.

    ONE ROW PER ORGANIZATION. The sweep pays per lead, and gold alone holds ~30
    duplicated entity names — the first production run scraped Modesto City Schools
    twice and Mt. Morris three times, buying the same page over and over. Grouping on
    the canonical key means each organization is fetched once; the profile is stored
    per lead, so the duplicates are picked up on a later pass rather than paid for
    twice in this one.

    ORDERING IS BY AWARD AMOUNT, and the honest caveat is that `amount` is NULL on
    most gold rows, so in practice this degrades to id order. `lead_score` would be
    the right key and cannot be used — it is a computed function in `scoring.py`, not
    a column, and ordering by it in SQL fails outright. Said plainly rather than left
    as a claim the data does not support.
    """
    return list(
        conn.execute(
            """SELECT MIN(id) AS id, entity_name, state, amount
                 FROM leads
                WHERE lead_grade = ?
                  AND COALESCE(org_profile_status,'') <> 'found'
                  AND COALESCE(entity_name,'') <> ''
                GROUP BY COALESCE(NULLIF(canonical_entity_key,''), entity_name)
                ORDER BY COALESCE(MAX(amount),0) DESC, MIN(id)
                LIMIT ?""",
            (grade, max(1, limit)),
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
