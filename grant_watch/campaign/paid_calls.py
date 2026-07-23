"""Durable pre-HTTP state for possibly paid rich-card enrichment.

An ``in_flight`` row is committed before the callback begins. If the process dies, the
row remains indeterminate and a later run refuses to retry unless an operator supplies
``retry_indeterminate=True``. This prevents silent double-spend after restart.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

T = TypeVar("T")


class IndeterminatePaidCall(RuntimeError):
    """A prior paid operation may have executed and needs operator reconciliation."""


class CompletedPaidCall(RuntimeError):
    """The exact bounded operation already completed and must not repeat."""


def execute(
    conn: sqlite3.Connection,
    lead_id: int,
    operation: str,
    request_key: str,
    work: Callable[[], T],
    *,
    retry_indeterminate: bool = False,
) -> T:
    """Reserve durably, execute once, and finalize the known callback outcome."""
    prior = conn.execute(
        """SELECT * FROM paid_enrichment_attempts WHERE request_key=?
           ORDER BY attempt_no DESC LIMIT 1""",
        (request_key,),
    ).fetchone()
    if prior is not None and prior["state"] == "completed":
        raise CompletedPaidCall("this paid preparation operation already completed")
    if prior is not None and prior["state"] == "in_flight":
        if not retry_indeterminate:
            raise IndeterminatePaidCall(
                "prior paid operation is indeterminate; explicit retry required"
            )
        with conn:
            conn.execute(
                """UPDATE paid_enrichment_attempts
                   SET state='indeterminate',finished_at=? WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), prior["id"]),
            )
    attempt_no = int(prior["attempt_no"]) + 1 if prior is not None else 1
    attempt_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """INSERT INTO paid_enrichment_attempts
                 (id,lead_id,operation,request_key,attempt_no,state,started_at)
               VALUES (?,?,?,?,?,'in_flight',?)""",
            (attempt_id, lead_id, operation, request_key, attempt_no, started),
        )
    try:
        result = work()
    except Exception as exc:
        with conn:
            conn.execute(
                """UPDATE paid_enrichment_attempts
                   SET state='failed',finished_at=?,error=? WHERE id=?""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    type(exc).__name__,
                    attempt_id,
                ),
            )
        raise
    with conn:
        conn.execute(
            """UPDATE paid_enrichment_attempts
               SET state='completed',finished_at=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), attempt_id),
        )
    return result
