"""Batch contact enrichment for search results — the paid, slow half of a search.

Split from search.py at the 1000-line cap (rule 4). The responsibility is narrow and
worth isolating: this is the only place a read-only search turns into paid, wall-clock
-bounded work, and the only place per-organization failure has to degrade into an
honest cell instead of sinking the batch.

The contact columns and their single constructor live here together on purpose —
several consumers unpack the first four positionally, so the column tuple and the
cell builder must never drift apart.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from .. import db

Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop


# A REP MUST BE ABLE TO ENRICH A WHOLE CAMPAIGN'S WORTH IN ONE ASK. This was 10,
# with no offset and a deterministic sort (recency, then amount, then id), so a
# repeat call with the same filters returned the SAME top ten: on 2026-08-11 Grant told a rep "the tool keeps returning the same
# top 10 regardless of the filter I add" and simply could not reach the other four
# organizations of fourteen. Ten was never a Salesforce or vendor limit — it was a
# wall-clock guess made when this loop was sequential.
MAX_ENRICH_ROWS = 100
# Lookups are network-bound and independent per organization, so they run in a
# bounded pool. Each worker opens its OWN SQLite connection: `enrich_lead_contact`
# writes (contacts, and the paid-attempt reservation), and one connection shared
# across threads is not safe. WAL plus busy_timeout=10s covers the commit overlap,
# and the paid ledger is keyed per lead so two workers can never reserve the same
# spend.
MAX_ENRICH_WORKERS = 8
# This is a higher-level ask ceiling, separate from the account's persisted monthly
# budget. It prevents a 100-row request from expanding into an unbounded sequence of
# search + page calls when each organization takes several fallback rungs.
MAX_FIRECRAWL_CALLS_PER_BATCH = 200


def configured_enrich_workers() -> int:
    """Return a validated worker count without making module import fragile."""
    raw = os.environ.get("GRANT_ENRICH_WORKERS", "8").strip()
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError("GRANT_ENRICH_WORKERS must be an integer from 1 to 8") from exc
    if not 1 <= workers <= MAX_ENRICH_WORKERS:
        raise ValueError("GRANT_ENRICH_WORKERS must be an integer from 1 to 8")
    return workers


ENRICH_TIME_BUDGET_S = (
    420.0  # stop STARTING new lookups past this wall-clock; disclose the partial.
    # 240 -> 420 when the per-org fallback chain (LinkedIn + org mailbox) landed.
    # Deliberately NOT raised for the 10 -> 100 ceiling: eight workers are what make
    # 100 reachable, not a longer wall clock, and this budget has to sit inside the
    # watchdog's STUCK_AFTER along with the model turns or a live enrichment gets
    # reported to the rep as a lost conversation.
)

_CONTACT_COLUMNS = (
    "contact_name",
    "contact_title",
    "contact_email",
    "contact_status",
    # Appended LAST, deliberately: several consumers unpack the first four
    # positionally, so inserting anywhere else silently rebinds `contact_status`
    # to a phone number. These are two DIFFERENT facts and are never merged —
    # contact_phone is the named person's own line, verified verbatim on the page
    # it came from; org_phone is the organization's main switchboard. Putting a
    # switchboard next to a person's name reads as their direct number, which is
    # a claim no source supports (rule 1).
    "contact_phone",
    "org_phone",
)
# Padding width for cells built by _enrich_contacts; every cell is this wide.
_CONTACT_CELL_WIDTH = len(_CONTACT_COLUMNS)


def _contact_cell(
    *,
    name: str = "",
    title: str = "",
    email: str = "",
    status: str = "",
    phone: str = "",
    org_phone: str = "",
) -> list[object]:
    """Build ONE enriched-contact cell, always exactly _CONTACT_COLUMNS wide.

    There used to be three literal cell shapes here — success, time-budget, and
    error — so widening the columns meant remembering all three, and missing one
    left those rows short against the header, silently shifting every later column
    in the export. One constructor makes that class of bug unrepresentable.
    """
    cell: list[object] = [name, title, email, status, phone, org_phone]
    assert len(cell) == _CONTACT_CELL_WIDTH, "cell width must match _CONTACT_COLUMNS"
    return cell


def _enrich_contacts(
    rows: list[sqlite3.Row],
    db_target: Path | str,
    requested_limit: int,
    on_progress: Progress | None,
) -> tuple[list[list[object]], str]:
    """Find each shown org's best contact on ONE writable connection, honestly and
    within a wall-clock budget. Returns per-row [name, title, email, status] cells (one
    per input row, always) plus a disclosure note. Runs AFTER the read-only snapshot is
    closed. Per-org failures degrade to an explicit cell, never sink the batch or
    fabricate a contact; an unreachable source records nothing (retryable)."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from ..enrich import firecrawl_gateway
    from . import tools  # local import: avoids the tools<->search cycle at module load

    say = on_progress or _NOOP
    deadline = time.monotonic() + ENRICH_TIME_BUDGET_S
    lock = threading.Lock()
    done = 0
    skipped = 0
    call_budget_skipped = 0
    call_budget = firecrawl_gateway.FirecrawlCallBudget(MAX_FIRECRAWL_CALLS_PER_BATCH)

    def enrich_one(row: sqlite3.Row) -> list[object]:
        """Look up ONE organization on its own connection; never raise into the pool.

        The connection is opened AND closed inside the worker. A `sqlite3`
        connection is bound to the thread that created it, so a pool-wide handle
        cached in a `threading.local` cannot be cleaned up afterwards from the main
        thread — that raised `ProgrammingError` and the suite caught it. Reopening
        costs a schema-version check against a 30-second network lookup.
        """
        nonlocal call_budget_skipped, done, skipped
        if time.monotonic() > deadline:
            with lock:
                skipped += 1
            return _contact_cell(status="not checked (time budget)")
        conn = db.connect(db_target)
        try:
            with firecrawl_gateway.use_call_budget(call_budget):
                outcome = tools.enrich_lead_contact(conn, int(row["id"]), None)
            cell = _contact_cell(
                name=outcome.name,
                title=outcome.title,
                email=outcome.email,
                status=outcome.status,
                phone=outcome.phone,
                org_phone=outcome.org_phone,
            )
        except (
            firecrawl_gateway.FirecrawlBudgetExhausted,
            firecrawl_gateway.FirecrawlBudgetNotConfigured,
        ):
            with lock:
                call_budget_skipped += 1
            cell = _contact_cell(status="not checked (call budget)")
        except Exception:  # noqa: BLE001 — one org's failure must not sink the batch
            cell = _contact_cell(status="error")
        finally:
            conn.close()
        with lock:
            done += 1
            position = done
        # `say` performs a Slack chat_update. Calling it INSIDE the lock serialized
        # all eight workers behind one network round-trip each, which is how a pool
        # quietly becomes sequential. The counter is still read under the lock, so
        # the number itself is never torn.
        say(f"Looking for contacts ({position}/{len(rows)})")
        return cell

    workers = min(configured_enrich_workers(), len(rows))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # `map` preserves input order, which the caller zips against its rows.
        cells = list(pool.map(enrich_one, rows))
    notes: list[str] = []
    if requested_limit > MAX_ENRICH_ROWS:
        notes.append(
            f"Contacts are capped at {MAX_ENRICH_ROWS} organizations per search."
        )
    if skipped:
        # RESUMABILITY IS THE POINT OF SAYING THIS — but only as far as it is true.
        # A completed lookup is cached, so asking again does continue rather than
        # repeat. An organization whose source was UNREACHABLE is not cached: that
        # attempt is filed retryable on purpose, so it is checked again and may
        # cost again. The first version of this sentence promised "costs nothing
        # extra" for exactly the population an 8-worker burst produces most of.
        notes.append(
            f"{done} of {len(rows)} organizations were checked before the time "
            f"budget and {skipped} were not; ask again and I'll carry on with "
            "those. Ones already checked are cached and cost nothing to repeat; "
            "any whose source was unreachable are retried properly."
        )
    if call_budget_skipped:
        notes.append(
            f"The fixed {MAX_FIRECRAWL_CALLS_PER_BATCH}-call enrichment budget was "
            f"reached; {call_budget_skipped} organization(s) were not fully checked."
        )
    return cells, (" (" + " ".join(notes) + ")" if notes else "")
