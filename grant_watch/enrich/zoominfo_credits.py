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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo

from ..paid_provider_authority import (
    PaidProviderAuthorityError,
    ProviderBinding,
    load_binding,
    require_call_authority,
)

T = TypeVar("T")

# The contract's reset day and timezone are NOT yet confirmed in writing, so the
# period is a calendar month in this timezone and is labelled `assumed` wherever it
# is reported. Aligning it to the real contract is a one-line change here.
PERIOD_TZ = ZoneInfo("America/Los_Angeles")
LEDGER_PATH_ENV = "ZOOMINFO_CREDIT_LEDGER_PATH"
LEDGER_SCHEMA_VERSION = 2


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


class LedgerMigrationError(ZoomInfoBudgetError):
    """Legacy credit state cannot be copied without weakening the spend guard."""


@dataclass(frozen=True)
class LedgerMigrationSummary:
    """Non-sensitive totals proving what one ledger migration preserved."""

    periods: int
    spends: int
    consumed_credits: int
    reserved_credits: int
    billed_credits: int
    already_migrated: bool = False
    sources: int = 1


@dataclass(frozen=True)
class LegacyLedgerSource:
    """One explicitly named legacy vendor-account history to reconcile."""

    source_scope: str
    connection: sqlite3.Connection


def _ledger_path() -> Path:
    """Return the explicit grants-only account ledger path or fail closed."""
    raw = os.environ.get(LEDGER_PATH_ENV, "").strip()
    if not raw:
        raise BudgetNotConfigured(
            f"{LEDGER_PATH_ENV} is not set, so account-wide spend cannot be enforced"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise BudgetNotConfigured(f"{LEDGER_PATH_ENV} must be an absolute path")
    return path


def _initialize_ledger(conn: sqlite3.Connection, binding: ProviderBinding) -> None:
    """Create the versioned standalone ledger schema idempotently."""
    if binding.provider != "zoominfo":
        raise LedgerMigrationError(
            "ZoomInfo ledger received the wrong provider binding"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS zoominfo_ledger_schema (
          version INTEGER PRIMARY KEY,
          applied_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS zoominfo_ledger_metadata (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          provider TEXT NOT NULL CHECK(provider='zoominfo'),
          authority_id TEXT NOT NULL,
          account_scope_id TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS zoominfo_credit_periods (
          period TEXT PRIMARY KEY,
          credit_limit INTEGER NOT NULL CHECK (credit_limit >= 0),
          consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
          updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS zoominfo_credit_spends (
          id TEXT PRIMARY KEY,
          period TEXT NOT NULL,
          request_key TEXT NOT NULL UNIQUE,
          requested_by TEXT NOT NULL DEFAULT '',
          lead_id INTEGER,
          reserved_credits INTEGER NOT NULL CHECK (reserved_credits > 0),
          billed_credits INTEGER,
          state TEXT NOT NULL CHECK (
            state IN ('reserved','settled','indeterminate','released')
          ),
          started_at TIMESTAMP NOT NULL,
          finished_at TIMESTAMP,
          error TEXT,
          source_scope TEXT NOT NULL DEFAULT 'runtime',
          legacy_id TEXT,
          legacy_request_key TEXT,
          FOREIGN KEY (period) REFERENCES zoominfo_credit_periods(period)
        );
        CREATE INDEX IF NOT EXISTS ix_zoominfo_spends_period_state
          ON zoominfo_credit_spends(period, state);
        """
    )
    conn.execute(
        """INSERT OR IGNORE INTO zoominfo_ledger_schema(version,applied_at)
           VALUES (?,?)""",
        (LEDGER_SCHEMA_VERSION, _now()),
    )
    conn.execute(
        """INSERT OR IGNORE INTO zoominfo_ledger_metadata
             (singleton,provider,authority_id,account_scope_id,created_at)
           VALUES (1,'zoominfo',?,?,?)""",
        (binding.authority_id, binding.account_scope_id, _now()),
    )
    conn.commit()


def _validate_ledger_schema(conn: sqlite3.Connection, binding: ProviderBinding) -> None:
    """Refuse an absent or unknown ledger schema instead of repairing it silently."""
    try:
        row = conn.execute("SELECT MAX(version) FROM zoominfo_ledger_schema").fetchone()
        metadata = conn.execute(
            """SELECT provider,authority_id,account_scope_id
                 FROM zoominfo_ledger_metadata WHERE singleton=1"""
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise BudgetNotConfigured(
            "ZoomInfo ledger is not initialized; run the reviewed ledger migration"
        ) from exc
    if row is None or row[0] != LEDGER_SCHEMA_VERSION or metadata is None:
        raise BudgetNotConfigured(
            "ZoomInfo ledger schema is missing or unsupported; spend remains disabled"
        )
    if tuple(metadata) != (
        "zoominfo",
        binding.authority_id,
        binding.account_scope_id,
    ):
        raise BudgetNotConfigured(
            "ZoomInfo ledger does not match this host/account authority"
        )


def _connect_existing_ledger(
    path: Path, binding: ProviderBinding
) -> sqlite3.Connection:
    """Open one pre-initialized private ledger without creating an empty authority."""
    if path.is_symlink() or not path.is_file():
        raise BudgetNotConfigured(
            "ZoomInfo ledger does not exist; migrate legacy usage before enabling spend"
        )
    if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise BudgetNotConfigured(
            "ZoomInfo ledger must be owned by this tenant and private to its user"
        )
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _validate_ledger_schema(conn, binding)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:
        conn.close()
        raise
    return conn


def connect_ledger() -> sqlite3.Connection:
    """Open the configured account ledger shared by every Grant app database.

    Runtime deliberately refuses to create a missing file. That makes a bad cutover
    fail closed instead of replacing historical usage with a visible balance of zero.
    """
    if configured_limit() <= 0:
        raise BudgetNotConfigured(
            "ZOOMINFO_MONTHLY_CREDITS is not set, so vendor access remains disabled"
        )
    try:
        binding = require_call_authority(
            "zoominfo", ("ZOOMINFO_CLIENT_ID", "ZOOMINFO_CLIENT_SECRET")
        )
    except PaidProviderAuthorityError as exc:
        raise BudgetNotConfigured(str(exc)) from exc
    return _connect_existing_ledger(_ledger_path(), binding)


def validate_provider_call() -> None:
    """Prove authority, credentials, ceiling, and ledger binding before HTTP."""
    ledger = connect_ledger()
    ledger.close()


def initialize_empty_ledger(path: Path, binding: ProviderBinding | None = None) -> None:
    """Create a new private zero-usage ledger for a confirmed new vendor account.

    This explicit bootstrap exists for tests and genuinely new accounts. Production
    upgrades with embedded history must use :func:`migrate_legacy_ledger` instead.
    """
    resolved = path.expanduser()
    if not resolved.is_absolute():
        raise LedgerMigrationError("ledger destination must be an absolute path")
    if resolved.exists():
        raise LedgerMigrationError("refusing to replace an existing ledger")
    selected = binding or load_binding("zoominfo")
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = resolved.with_name(f".{resolved.name}.{uuid.uuid4().hex}.tmp")
    try:
        conn = sqlite3.connect(temp_path, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            _initialize_ledger(conn, selected)
        finally:
            conn.close()
        temp_path.chmod(0o600)
        _publish_without_replacement(temp_path, resolved)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _publish_without_replacement(temp_path: Path, destination: Path) -> None:
    """Atomically expose a sibling ledger only when the target is still absent.

    ``Path.replace`` would overwrite a ledger created between the initial existence
    check and publication. A same-directory hard link is atomic and refuses that
    race, preserving whichever authority appeared first.
    """
    try:
        os.link(temp_path, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise LedgerMigrationError(
            "ledger destination appeared during creation; refusing to replace it"
        ) from exc
    file_descriptor = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    directory_descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def inspect_legacy_ledger(conn: sqlite3.Connection) -> LedgerMigrationSummary:
    """Validate embedded legacy state and return only safe aggregate totals."""
    from ..zoominfo_ledger_state import inspect_legacy_ledger as inspect

    return inspect(conn)


def inspect_legacy_ledgers(
    sources: Sequence[LegacyLedgerSource],
) -> LedgerMigrationSummary:
    """Validate and reconcile all explicitly scoped legacy histories read-only."""
    from ..zoominfo_ledger_state import inspect_legacy_ledgers as inspect

    return inspect(sources)


def migrate_legacy_ledger(
    source: sqlite3.Connection, destination_path: Path
) -> LedgerMigrationSummary:
    """Preserve one legacy ledger using a deterministic path-derived scope."""
    from ..zoominfo_ledger_state import default_source_scope, migrate_legacy_ledgers

    return migrate_legacy_ledgers(
        (LegacyLedgerSource(default_source_scope(source), source),), destination_path
    )


def migrate_legacy_ledgers(
    sources: Sequence[LegacyLedgerSource], destination_path: Path
) -> LedgerMigrationSummary:
    """Lock and reconcile all same-account histories into one authority ledger."""
    from ..zoominfo_ledger_state import migrate_legacy_ledgers as migrate

    return migrate(sources, destination_path)


@contextmanager
def _ledger() -> Iterator[sqlite3.Connection]:
    """Yield and close the configured account-wide ledger connection."""
    conn = connect_ledger()
    try:
        yield conn
    finally:
        conn.close()


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
    """Credits still available in the shared account ledger (never negative)."""
    del conn  # application DB is deliberately not the account authorization store
    key = period or current_period()
    try:
        with _ledger() as ledger:
            row = ledger.execute(
                """SELECT credit_limit-consumed FROM zoominfo_credit_periods
                   WHERE period=?""",
                (key,),
            ).fetchone()
    except BudgetNotConfigured:
        return 0
    if row is None:
        return configured_limit()
    return max(0, int(row[0]))


def usage(conn: sqlite3.Connection, period: str | None = None) -> tuple[int, int]:
    """Return account-wide (consumed, limit) for display and approval."""
    del conn  # application DB is deliberately not the account authorization store
    key = period or current_period()
    try:
        with _ledger() as ledger:
            row = ledger.execute(
                """SELECT consumed,credit_limit FROM zoominfo_credit_periods
                   WHERE period=?""",
                (key,),
            ).fetchone()
    except BudgetNotConfigured:
        return 0, 0
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
    del conn  # every app instance authorizes against the same configured ledger
    limit = configured_limit()
    if limit <= 0:
        raise BudgetNotConfigured(
            "ZOOMINFO_MONTHLY_CREDITS is not set, so no paid pull can be authorized"
        )
    period = current_period(now)
    spend_id = uuid.uuid4().hex
    with _ledger() as ledger:
        ensure_period(ledger, period, limit)
        ledger.execute("BEGIN IMMEDIATE")
        try:
            prior = _prior(ledger, request_key)
            if prior is not None:
                ledger.rollback()
                if prior["state"] == "settled":
                    raise AlreadySpent("this ZoomInfo pull already completed")
                if prior["state"] in {"reserved", "indeterminate"}:
                    raise SpendIndeterminate(
                        "a prior attempt on this pull may have billed; "
                        "reconcile before retry"
                    )
                raise AlreadySpent(
                    "this ZoomInfo pull key already exists; use a new human approval"
                )
            claimed = ledger.execute(
                """UPDATE zoominfo_credit_periods
                      SET consumed=consumed+?, updated_at=?
                    WHERE period=? AND consumed+? <= credit_limit""",
                (credits, _now(), period, credits),
            ).rowcount
            if claimed != 1:
                ledger.rollback()
                row = ledger.execute(
                    """SELECT consumed,credit_limit FROM zoominfo_credit_periods
                       WHERE period=?""",
                    (period,),
                ).fetchone()
                consumed, ceiling = (int(row[0]), int(row[1])) if row else (0, limit)
                raise BudgetExhausted(
                    f"{credits} credits requested but only "
                    f"{max(0, ceiling - consumed)} remain in {period}"
                )
            ledger.execute(
                """INSERT INTO zoominfo_credit_spends
                     (id,period,request_key,requested_by,lead_id,reserved_credits,
                      state,started_at)
                   VALUES (?,?,?,?,?,?, 'reserved', ?)""",
                (
                    spend_id,
                    period,
                    request_key,
                    requested_by,
                    lead_id,
                    credits,
                    _now(),
                ),
            )
            ledger.commit()
        except (AlreadySpent, BudgetExhausted, SpendIndeterminate):
            raise
        except Exception:
            ledger.rollback()
            raise
    return spend_id


def settle(conn: sqlite3.Connection, spend_id: str, billed: int) -> None:
    """Record what the vendor actually billed and refund the proven difference.

    A NO_MATCH costs nothing, so a pull that reserved nine and matched five gives
    four back. Only this proven-unbilled path ever returns credits.
    """
    del conn
    with _ledger() as ledger:
        ledger.execute("BEGIN IMMEDIATE")
        row = ledger.execute(
            "SELECT * FROM zoominfo_credit_spends WHERE id=?", (spend_id,)
        ).fetchone()
        if row is None:
            ledger.rollback()
            raise ValueError(f"unknown ZoomInfo spend {spend_id}")
        reserved = int(row["reserved_credits"])
        if billed < 0 or billed > reserved:
            ledger.rollback()
            raise ValueError("billed credits must be within the approved reservation")
        if row["state"] == "settled":
            ledger.rollback()
            if int(row["billed_credits"] or 0) == billed:
                return
            raise ValueError("settled ZoomInfo spend cannot be reconciled twice")
        if row["state"] not in {"reserved", "indeterminate"}:
            ledger.rollback()
            raise ValueError(f"cannot settle ZoomInfo spend in state {row['state']}")
        refund = reserved - billed
        try:
            if refund:
                ledger.execute(
                    """UPDATE zoominfo_credit_periods
                          SET consumed=MAX(0, consumed-?), updated_at=?
                        WHERE period=?""",
                    (refund, _now(), row["period"]),
                )
            ledger.execute(
                """UPDATE zoominfo_credit_spends
                      SET state='settled', billed_credits=?, finished_at=?
                    WHERE id=?""",
                (billed, _now(), spend_id),
            )
            ledger.commit()
        except Exception:
            ledger.rollback()
            raise


def mark_indeterminate(conn: sqlite3.Connection, spend_id: str, error: str) -> None:
    """Record an attempt that may or may not have billed, keeping it counted.

    Deliberately no refund: a timeout is not evidence the vendor did nothing, and
    quietly returning the credits would let the same pull be retried into a real
    double-spend.
    """
    del conn
    with _ledger() as ledger, ledger:
        updated = ledger.execute(
            """UPDATE zoominfo_credit_spends
                  SET state='indeterminate', finished_at=?, error=?
                WHERE id=? AND state='reserved'""",
            (_now(), error[:200], spend_id),
        )
        if updated.rowcount != 1:
            row = ledger.execute(
                "SELECT state FROM zoominfo_credit_spends WHERE id=?", (spend_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown ZoomInfo spend {spend_id}")
            if row["state"] != "indeterminate":
                raise ValueError(
                    f"cannot mark ZoomInfo spend indeterminate from {row['state']}"
                )


def release(conn: sqlite3.Connection, spend_id: str, reason: str) -> None:
    """Return credits for a pull PROVEN never to have reached the vendor.

    Only for refusals raised before any HTTP call — never for a failure that
    happened after the request left the process.
    """
    del conn
    with _ledger() as ledger:
        # Serialize the state check with the refund. A read followed by a later
        # transaction lets two refusal handlers both observe `reserved` and
        # manufacture a second refund.
        ledger.execute("BEGIN IMMEDIATE")
        try:
            row = ledger.execute(
                "SELECT * FROM zoominfo_credit_spends WHERE id=?", (spend_id,)
            ).fetchone()
            if row is None or row["state"] != "reserved":
                ledger.rollback()
                return
            ledger.execute(
                """UPDATE zoominfo_credit_periods
                      SET consumed=MAX(0, consumed-?), updated_at=? WHERE period=?""",
                (int(row["reserved_credits"]), _now(), row["period"]),
            )
            ledger.execute(
                """UPDATE zoominfo_credit_spends
                      SET state='released', billed_credits=0, finished_at=?, error=?
                    WHERE id=?""",
                (_now(), reason[:200], spend_id),
            )
            ledger.commit()
        except Exception:
            ledger.rollback()
            raise


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
