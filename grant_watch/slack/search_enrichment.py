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

import sqlite3
from collections.abc import Callable
from pathlib import Path

from .. import db

Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop


MAX_ENRICH_ROWS = 10  # hard ceiling on per-search contact lookups (cost + latency)
ENRICH_TIME_BUDGET_S = (
    420.0  # stop enriching past this wall-clock; disclose the partial. Raised
    # from 240 when the per-org fallback chain (LinkedIn + org mailbox) landed.
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
    import time

    from . import tools  # local import: avoids the tools<->search cycle at module load

    say = on_progress or _NOOP
    cells: list[list[object]] = []
    conn = db.connect(db_target)
    deadline = time.monotonic() + ENRICH_TIME_BUDGET_S
    try:
        for index, row in enumerate(rows, start=1):
            if time.monotonic() > deadline:
                cells.append(_contact_cell(status="not checked (time budget)"))
                continue
            say(f"Looking for contacts ({index}/{len(rows)})")
            try:
                outcome = tools.enrich_lead_contact(conn, int(row["id"]), say)
                cells.append(
                    _contact_cell(
                        name=outcome.name,
                        title=outcome.title,
                        email=outcome.email,
                        status=outcome.status,
                        phone=outcome.phone,
                        org_phone=outcome.org_phone,
                    )
                )
            except Exception:  # noqa: BLE001 — one org's failure must not sink the batch
                cells.append(_contact_cell(status="error"))
    finally:
        conn.close()
    note = (
        f" (Contacts limited to the top {MAX_ENRICH_ROWS} to stay responsive.)"
        if requested_limit > MAX_ENRICH_ROWS
        else ""
    )
    return cells, note
