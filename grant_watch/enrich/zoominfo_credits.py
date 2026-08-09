"""The spend gate in front of ZoomInfo's paid enrichment.

WHY A LEDGER AND NOT A COUNTER. ZoomInfo bills the ACCOUNT, not a person, and Grant
runs in Slack where two reps can act at the same moment. A read-then-spend pattern
lets two threads both read "991 used" and both spend nine, so the authorization here
is a CONDITIONAL UPDATE whose rowcount IS the permission — nothing is spent unless
the database itself agreed there was room, in the same transaction that records why.

WHY IT FAILS TOWARD OVERCOUNTING. Once the HTTP call begins, a timeout cannot prove
whether the vendor billed. Those attempts stay `indeterminate` and stay counted
against the period: overstating spend costs a rep some headroom, understating it
overdraws a shared company resource. Credits are only given back when the vendor's
own response proves fewer records were billable (a NO_MATCH is free), or when the
call provably never happened.

WHAT THIS DELIBERATELY DOES NOT DO. It does not authorize anything on its own. A
paid pull is quoted from FREE search results and approved by a human first; this
module is what makes the approved number and the spent number the same number.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar
from zoneinfo import ZoneInfo

T = TypeVar("T")

# The contract's reset day and timezone are NOT yet confirmed in writing, so the
# period is a calendar month in this timezone and is labelled `assumed` wherever it
# is reported. Aligning it to the real contract is a one-line change here.
PERIOD_TZ = ZoneInfo("America/Los_Angeles")


class ZoomInfoBudgetError(RuntimeError):
    """Base class for every refusal that protects the shared credit pool."""


class BudgetNotConfigured(ZoomInfoBudgetError):
    """No monthly credit limit is configured, so no spend may be authorized."""


class BudgetExhausted(ZoomInfoBudgetError):
    """The period has fewer credits left than this pull would consume."""


class AlreadySpent(ZoomInfoBudgetError):
    """This exact bounded pull already completed and must not repeat."""


class SpendIndeterminate(ZoomInfoBudgetError):
    """A prior attempt on this key may have billed; an operator must reconcile."""


def _now() -> str:
    """Return an ISO UTC timestamp for ledger rows."""
    return datetime.now(timezone.utc).isoformat()


def configured_limit() -> int:
    """Read the account-wide monthly credit ceiling, 0 when unset.

    Account-wide on purpose: a per-user allowance is fiction when the vendor bills
    one pool, and two reps each believing they hold 500 can exhaust 1,000 between
    them. Per-user numbers are for display, never for authorization.
    """
    raw = os.environ.get("ZOOMINFO_MONTHLY_CREDITS", "").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def current_period(now: datetime | None = None) -> str:
    """Return the ledger period key (calendar month in PERIOD_TZ)."""
    moment = (now or datetime.now(timezone.utc)).astimezone(PERIOD_TZ)
    return f"{moment.year:04d}-{moment.month:02d}"


def ensure_period(conn: sqlite3.Connection, period: str, limit: int) -> None:
    """Create the period row if absent and keep its limit current.

    The limit is refreshed rather than frozen so raising the ceiling in `.env` takes
    effect without a migration; `consumed` is never touched here.
    """
    with conn:
        conn.execute(
            """INSERT INTO zoominfo_credit_periods (period,credit_limit,consumed,updated_at)
               VALUES (?,?,0,?)
               ON CONFLICT(period) DO UPDATE SET credit_limit=excluded.credit_limit,
                                                updated_at=excluded.updated_at""",
            (period, limit, _now()),
        )


def remaining(conn: sqlite3.Connection, period: str | None = None) -> int:
    """Credits still available this period (never negative)."""
    key = period or current_period()
    row = conn.execute(
        "SELECT credit_limit-consumed FROM zoominfo_credit_periods WHERE period=?",
        (key,),
    ).fetchone()
    if row is None:
        return configured_limit()
    return max(0, int(row[0]))


def usage(conn: sqlite3.Connection, period: str | None = None) -> tuple[int, int]:
    """Return (consumed, limit) for display — the numbers a warning quotes."""
    key = period or current_period()
    row = conn.execute(
        "SELECT consumed,credit_limit FROM zoominfo_credit_periods WHERE period=?",
        (key,),
    ).fetchone()
    if row is None:
        return 0, configured_limit()
    return int(row[0]), int(row[1])


def _prior(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    """Return any existing ledger row for this exact bounded pull."""
    return conn.execute(
        "SELECT * FROM zoominfo_credit_spends WHERE request_key=?", (request_key,)
    ).fetchone()


def reserve(
    conn: sqlite3.Connection,
    *,
    request_key: str,
    credits: int,
    requested_by: str = "",
    lead_id: int | None = None,
    now: datetime | None = None,
) -> str:
    """Claim `credits` atomically for this period, returning the ledger row id.

    The conditional UPDATE is the authorization: if the period lacks room its
    rowcount is 0 and nothing is reserved, so two concurrent callers can never both
    win the last credit. The whole approved quantity is claimed at once — a partial
    reservation would make "this will use 9" untrue the moment row 6 exhausted the
    pool, leaving a rep who approved a set with an arbitrary prefix of it.
    """
    if credits <= 0:
        raise ValueError("a ZoomInfo reservation must claim at least one credit")
    limit = configured_limit()
    if limit <= 0:
        raise BudgetNotConfigured(
            "ZOOMINFO_MONTHLY_CREDITS is not set, so no paid pull can be authorized"
        )
    period = current_period(now)
    ensure_period(conn, period, limit)

    prior = _prior(conn, request_key)
    if prior is not None:
        if prior["state"] == "settled":
            raise AlreadySpent("this ZoomInfo pull already completed")
        if prior["state"] in {"reserved", "indeterminate"}:
            raise SpendIndeterminate(
                "a prior attempt on this pull may have billed; reconcile before retry"
            )

    spend_id = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        claimed = conn.execute(
            """UPDATE zoominfo_credit_periods
                  SET consumed=consumed+?, updated_at=?
                WHERE period=? AND consumed+? <= credit_limit""",
            (credits, _now(), period, credits),
        ).rowcount
        if claimed != 1:
            conn.rollback()
            consumed, ceiling = usage(conn, period)
            raise BudgetExhausted(
                f"{credits} credits requested but only {max(0, ceiling - consumed)} "
                f"remain in {period}"
            )
        conn.execute(
            """INSERT INTO zoominfo_credit_spends
                 (id,period,request_key,requested_by,lead_id,reserved_credits,
                  state,started_at)
               VALUES (?,?,?,?,?,?, 'reserved', ?)""",
            (spend_id, period, request_key, requested_by, lead_id, credits, _now()),
        )
        conn.commit()
    except BudgetExhausted:
        raise
    except Exception:
        conn.rollback()
        raise
    return spend_id


def settle(conn: sqlite3.Connection, spend_id: str, billed: int) -> None:
    """Record what the vendor actually billed and refund the proven difference.

    A NO_MATCH costs nothing, so a pull that reserved nine and matched five gives
    four back. Only this proven-unbilled path ever returns credits.
    """
    row = conn.execute(
        "SELECT * FROM zoominfo_credit_spends WHERE id=?", (spend_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown ZoomInfo spend {spend_id}")
    refund = max(0, int(row["reserved_credits"]) - max(0, billed))
    with conn:
        if refund:
            conn.execute(
                """UPDATE zoominfo_credit_periods
                      SET consumed=MAX(0, consumed-?), updated_at=?
                    WHERE period=?""",
                (refund, _now(), row["period"]),
            )
        conn.execute(
            """UPDATE zoominfo_credit_spends
                  SET state='settled', billed_credits=?, finished_at=?
                WHERE id=?""",
            (max(0, billed), _now(), spend_id),
        )


def mark_indeterminate(conn: sqlite3.Connection, spend_id: str, error: str) -> None:
    """Record an attempt that may or may not have billed, keeping it counted.

    Deliberately no refund: a timeout is not evidence the vendor did nothing, and
    quietly returning the credits would let the same pull be retried into a real
    double-spend.
    """
    with conn:
        conn.execute(
            """UPDATE zoominfo_credit_spends
                  SET state='indeterminate', finished_at=?, error=?
                WHERE id=?""",
            (_now(), error[:200], spend_id),
        )


def release(conn: sqlite3.Connection, spend_id: str, reason: str) -> None:
    """Return credits for a pull PROVEN never to have reached the vendor.

    Only for refusals raised before any HTTP call — never for a failure that
    happened after the request left the process.
    """
    row = conn.execute(
        "SELECT * FROM zoominfo_credit_spends WHERE id=?", (spend_id,)
    ).fetchone()
    if row is None or row["state"] != "reserved":
        return
    with conn:
        conn.execute(
            """UPDATE zoominfo_credit_periods
                  SET consumed=MAX(0, consumed-?), updated_at=? WHERE period=?""",
            (int(row["reserved_credits"]), _now(), row["period"]),
        )
        conn.execute(
            """UPDATE zoominfo_credit_spends
                  SET state='released', billed_credits=0, finished_at=?, error=?
                WHERE id=?""",
            (_now(), reason[:200], spend_id),
        )


def spend(
    conn: sqlite3.Connection,
    *,
    request_key: str,
    credits: int,
    work: Callable[[], tuple[T, int]],
    requested_by: str = "",
    lead_id: int | None = None,
) -> T:
    """Reserve, run one paid pull, and settle it against what was really billed.

    `work` returns (result, billed_records). It is called exactly once and only
    after the reservation has committed, so a crash mid-call leaves the credits
    counted rather than silently available to spend again.
    """
    spend_id = reserve(
        conn,
        request_key=request_key,
        credits=credits,
        requested_by=requested_by,
        lead_id=lead_id,
    )
    try:
        result, billed = work()
    except Exception as exc:
        mark_indeterminate(conn, spend_id, type(exc).__name__)
        raise
    settle(conn, spend_id, billed)
    return result
