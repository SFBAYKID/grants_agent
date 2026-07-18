"""Typed, read-only on-demand search with honest source-specific date semantics.

Why: the canonical database reuses funds_start/funds_end for award spend windows,
Grants.gov application windows, and solicitation response windows. This module keeps
those meanings separate so Grant never calls an import date an award date or a spend
deadline an application close date.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import date
from enum import Enum
from pathlib import Path

from .. import db
from ..presentation import display_entity_name
from ..spreadsheets import GeneratedArtifact, make_spreadsheet
from .search_presentation import contact_suffix as _contact_suffix
from .search_presentation import entity_role_for_row as _entity_role_for_row
from .search_presentation import record_link as _record_link
from .search_presentation import window_label as _window_label

Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop

MAX_INLINE_LIMIT = 100
MAX_EXPORT_ROWS = 5_000
MAX_ENRICH_ROWS = 10  # hard ceiling on per-search contact lookups (cost + latency)
ENRICH_TIME_BUDGET_S = (
    240.0  # stop enriching past this wall-clock; disclose the partial
)

_CONTACT_COLUMNS = ("contact_name", "contact_title", "contact_email", "contact_status")

_SEARCH_COLUMNS = (
    "source",
    "source_item_id",
    "entity_name",
    "title",
    "entity_type",
    "state",
    "county",
    "program",
    "amount",
    "lead_grade",
    "funds_start",
    "funds_end",
    "first_seen",
    "last_seen",
    "status",
    "detail_url",
    "nces_id",
    "enrollment",
    "location_city",
    "location_confidence",
    "current_event_type",
    "current_event_occurred_on",
    "current_event_verification_status",
)

_SEARCH_CTE = """WITH searchable_leads AS (
    SELECT l.*,e.event_type AS current_event_type,
           e.occurred_on AS current_event_occurred_on,
           e.verification_status AS current_event_verification_status
      FROM leads l LEFT JOIN funding_events e ON e.id=l.current_event_id
)"""


class RecordKind(str, Enum):
    """Canonical record meanings exposed to Slack search."""

    AWARD = "award"
    FUNDING_OPPORTUNITY = "funding_opportunity"
    SOLICITATION = "solicitation"


class OrgType(str, Enum):
    """Organization categories supported by the deterministic classifier."""

    SCHOOL = "school"
    CITY = "city"
    COUNTY = "county"
    HOSPITAL = "hospital"
    ANY = "any"


class DateField(str, Enum):
    """Dates whose meanings are supported by the canonical database."""

    DISCOVERED = "discovered"
    OPPORTUNITY_OPEN = "opportunity_open"
    OPPORTUNITY_CLOSE = "opportunity_close"
    SOLICITATION_POSTED = "solicitation_posted"
    RESPONSE_DUE = "response_due"
    SPEND_START = "spend_start"
    SPEND_END = "spend_end"
    AWARD_RECEIVED = "award_received"


class ExportFormat(str, Enum):
    """Export destinations currently understood by Grant."""

    EXCEL = "excel"
    GOOGLE_SHEET = "google_sheet"


class ResultScope(str, Enum):
    """Whether a result/export contains the confirmed top N or every match."""

    TOP_N = "top_n"
    ALL = "all"


def _enum_value(enum_type: type[Enum], raw: str, label: str) -> str:
    """Validate an optional string against an enum and return its normalized value."""
    value = raw.strip().lower()
    if not value:
        return ""
    allowed = {str(item.value) for item in enum_type}
    if value not in allowed:
        raise ValueError(f"unknown {label} '{raw}'")
    return value


def _iso_date(raw: str, label: str) -> str:
    """Validate one optional inclusive ISO date bound without inventing a date."""
    value = raw.strip()
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc
    return value


def _like_literal(raw: str) -> str:
    """Escape SQLite LIKE metacharacters so user text remains a literal substring."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _school_name_clause() -> tuple[str, list[object]]:
    """Conservatively recognize school-specific names; generic DISTRICT is insufficient."""
    patterns = (
        "%SCHOOL%",
        "%ACADEMY%",
        "%CHARTER%",
        "% ISD",
        "% ISD %",
        "% USD",
        "% USD %",
        "%SCHOOL DISTRICT%",
    )
    clause = "(" + " OR ".join("UPPER(entity_name) LIKE ?" for _ in patterns) + ")"
    return clause, list(patterns)


def _org_clause(org_type: str) -> tuple[str, list[object]]:
    """Build a conservative org predicate, preferring stored type over name fallback."""
    if not org_type or org_type == "any":
        return "", []

    stored: dict[str, tuple[str, ...]] = {
        "school": ("school", "district", "school_district", "nonpublic_school"),
        "city": ("city", "town", "township", "borough", "village", "municipality"),
        "county": ("county",),
        "hospital": ("hospital", "health_system", "clinic"),
    }
    if org_type not in stored:
        raise ValueError(f"unknown organization type '{org_type}'")

    stored_values = stored[org_type]
    stored_sql = (
        "LOWER(COALESCE(entity_type, '')) IN ("
        + ",".join("?" for _ in stored_values)
        + ")"
    )
    params: list[object] = list(stored_values)
    school_sql, school_params = _school_name_clause()

    if org_type == "school":
        return f"({stored_sql} OR {school_sql})", params + school_params

    if org_type == "city":
        city_patterns = (
            "CITY OF %",
            "% CITY",
            "TOWN OF %",
            "% TOWN",
            "TOWNSHIP OF %",
            "% TOWNSHIP",
            "BOROUGH OF %",
            "% BOROUGH",
            "VILLAGE OF %",
            "% VILLAGE",
            "MUNICIPALITY OF %",
        )
        city_sql = (
            "(" + " OR ".join("UPPER(entity_name) LIKE ?" for _ in city_patterns) + ")"
        )
        return (
            f"({stored_sql} OR ({city_sql} AND NOT {school_sql}))",
            params + list(city_patterns) + school_params,
        )

    if org_type == "county":
        county_sql = (
            "(UPPER(entity_name) LIKE 'COUNTY OF %' OR "
            "UPPER(entity_name) LIKE '% COUNTY')"
        )
        return (
            f"({stored_sql} OR ({county_sql} AND NOT {school_sql}))",
            params + school_params,
        )

    hospital_patterns = (
        "% HOSPITAL%",
        "% HEALTH SYSTEM%",
        "% MEDICAL CENTER%",
        "% CLINIC%",
    )
    hospital_sql = (
        "(" + " OR ".join("UPPER(entity_name) LIKE ?" for _ in hospital_patterns) + ")"
    )
    return f"({stored_sql} OR {hospital_sql})", params + list(hospital_patterns)


def _record_clause(record_kind: str) -> tuple[str, list[object]]:
    """Map record-kind vocabulary to evidence-backed current event predicates."""
    if not record_kind:
        return "", []
    mapping = {
        RecordKind.AWARD.value: (
            "current_event_type IN ('award_announced','award_obligated')"
        ),
        RecordKind.FUNDING_OPPORTUNITY.value: (
            "current_event_type='application_window_opened'"
        ),
        RecordKind.SOLICITATION.value: "current_event_type='rfp_posted'",
    }
    if record_kind not in mapping:
        raise ValueError(f"unknown record kind '{record_kind}'")
    return mapping[record_kind], []


def _date_clause(
    date_field: str, date_from: str, date_to: str
) -> tuple[str, list[object], str]:
    """Map a validated date meaning to one source-aware SQL predicate and sort order."""
    if not date_field and not date_from and not date_to:
        return (
            "",
            [],
            "COALESCE(date(current_event_occurred_on),date(first_seen)) DESC, amount DESC",
        )
    if not date_field:
        raise ValueError("date_field is required with date_from/date_to")
    # date_field with no range is a valid sort-only ask ("newest verified award
    # announcements"): restrict to rows where that date meaning applies and order
    # by the field's canonical direction, with no range predicate.
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from cannot be after date_to")

    field_map = {
        DateField.DISCOVERED.value: (
            "date(first_seen)",
            "1=1",
            "date(first_seen) DESC",
        ),
        DateField.OPPORTUNITY_OPEN.value: (
            "date(current_event_occurred_on)",
            "current_event_type='application_window_opened'",
            "date(current_event_occurred_on) ASC",
        ),
        DateField.OPPORTUNITY_CLOSE.value: (
            "date(funds_end)",
            "current_event_type='application_window_opened'",
            "date(funds_end) ASC",
        ),
        DateField.SOLICITATION_POSTED.value: (
            "date(current_event_occurred_on)",
            "current_event_type='rfp_posted'",
            "date(current_event_occurred_on) DESC",
        ),
        DateField.RESPONSE_DUE.value: (
            "date(funds_end)",
            "current_event_type='rfp_posted'",
            "date(funds_end) ASC",
        ),
        DateField.SPEND_START.value: (
            "date(funds_start)",
            "current_event_type IN ('award_announced','award_obligated')",
            "date(funds_start) DESC",
        ),
        DateField.SPEND_END.value: (
            "date(funds_end)",
            "current_event_type IN ('award_announced','award_obligated')",
            "date(funds_end) ASC",
        ),
        DateField.AWARD_RECEIVED.value: (
            "date(current_event_occurred_on)",
            "current_event_type IN ('award_announced','award_obligated') "
            "AND current_event_verification_status='verified'",
            "date(current_event_occurred_on) DESC",
        ),
    }
    if date_field not in field_map:
        raise ValueError(f"unknown date field '{date_field}'")
    column, kind_sql, order_sql = field_map[date_field]
    clauses = [kind_sql, f"{column} IS NOT NULL"]
    params: list[object] = []
    if date_from:
        clauses.append(f"{column} >= date(?)")
        params.append(date_from)
    if date_to:
        clauses.append(f"{column} <= date(?)")
        params.append(date_to)
    return "(" + " AND ".join(clauses) + ")", params, order_sql


def _record_kind_for_row(row: sqlite3.Row) -> str:
    """Derive display/export meaning from the evidence-backed current event."""
    event_type = str(row["current_event_type"] or "")
    if event_type in {"award_announced", "award_obligated"}:
        return RecordKind.AWARD.value
    if event_type == "application_window_opened":
        return RecordKind.FUNDING_OPPORTUNITY.value
    if event_type == "rfp_posted":
        return RecordKind.SOLICITATION.value
    return "watch"


def _export_kind(raw: str | bool) -> str:
    """Normalize supported export values while retaining legacy True as Excel."""
    if raw is True:
        return ExportFormat.EXCEL.value
    if raw is False or raw == "":
        return ""
    return _enum_value(ExportFormat, str(raw), "export format")


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
                cells.append(["", "", "", "not checked (time budget)"])
                continue
            say(f"Looking for contacts ({index}/{len(rows)})")
            try:
                outcome = tools.enrich_lead_contact(conn, int(row["id"]), say)
                cells.append(
                    [outcome.name, outcome.title, outcome.email, outcome.status]
                )
            except Exception:  # noqa: BLE001 — one org's failure must not sink the batch
                cells.append(["", "", "", "error"])
    finally:
        conn.close()
    note = (
        f" (Contacts limited to the top {MAX_ENRICH_ROWS} to stay responsive.)"
        if requested_limit > MAX_ENRICH_ROWS
        else ""
    )
    return cells, note


def search_leads(
    state: str = "",
    org_type: str = "",
    program: str = "",
    grade: str = "",
    record_kind: str = "",
    amount_min: float | None = None,
    amount_max: float | None = None,
    enrollment_min: int | None = None,
    enrollment_max: int | None = None,
    city: str = "",
    name_contains: str = "",
    date_field: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    export: str | bool = "",
    result_scope: str = "top_n",
    with_contacts: bool = False,
    on_progress: Progress | None = None,
    requester_slack: str = "",
    workspace: str = "",
    channel: str = "",
    thread_ts: str = "",
    db_path: Path | str | None = None,
) -> tuple[str, GeneratedArtifact | None]:
    """Search one read-only SQLite snapshot and optionally export every matching row.

    with_contacts is the deliberate SECOND step (never automatic): it bounds the result
    to the top min(limit, MAX_ENRICH_ROWS) orgs, finds each one's verified-or-honest
    contact, and appends contact columns to the summary AND the export — so every shown
    row carries a real outcome instead of a misleading blank."""
    (on_progress or _NOOP)("Searching grant databases")
    try:
        org_value = _enum_value(OrgType, org_type, "organization type")
        record_value = _enum_value(RecordKind, record_kind, "record kind")
        date_value = _enum_value(DateField, date_field, "date field")
        export_value = _export_kind(export)
        scope_value = _enum_value(ResultScope, result_scope or "top_n", "result scope")
        from_value = _iso_date(date_from, "date_from")
        to_value = _iso_date(date_to, "date_to")
        if grade and grade.lower() not in {"gold", "silver", "watch"}:
            raise ValueError(f"unknown grade '{grade}'")
        if (
            amount_min is not None
            and amount_max is not None
            and amount_min > amount_max
        ):
            raise ValueError("amount_min cannot exceed amount_max")
        if enrollment_min is not None and enrollment_min < 0:
            raise ValueError("enrollment_min cannot be negative")
        if enrollment_max is not None and enrollment_max < 0:
            raise ValueError("enrollment_max cannot be negative")
        if (
            enrollment_min is not None
            and enrollment_max is not None
            and enrollment_min > enrollment_max
        ):
            raise ValueError("enrollment_min cannot exceed enrollment_max")
        compatible_kind = {
            DateField.OPPORTUNITY_OPEN.value: RecordKind.FUNDING_OPPORTUNITY.value,
            DateField.OPPORTUNITY_CLOSE.value: RecordKind.FUNDING_OPPORTUNITY.value,
            DateField.SOLICITATION_POSTED.value: RecordKind.SOLICITATION.value,
            DateField.RESPONSE_DUE.value: RecordKind.SOLICITATION.value,
            DateField.SPEND_START.value: RecordKind.AWARD.value,
            DateField.SPEND_END.value: RecordKind.AWARD.value,
        }.get(date_value)
        if record_value and compatible_kind and record_value != compatible_kind:
            raise ValueError(
                f"date field '{date_value}' is incompatible with record kind "
                f"'{record_value}'"
            )

        # Named filter groups: the zero-result path re-counts with one group
        # dropped at a time to offer honest widen/broaden alternatives.
        groups: list[tuple[str, str, list[object]]] = [
            ("base", "COALESCE(status, 'new') != 'dead'", [])
        ]
        if state:
            groups.append(("the state filter", "UPPER(state) = ?", [state.strip().upper()]))
        if program:
            groups.append(
                (
                    "the program filter",
                    "UPPER(program) LIKE ? ESCAPE '\\'",
                    [f"%{_like_literal(program.strip().upper())}%"],
                )
            )
        if grade:
            groups.append(("the grade filter", "lead_grade = ?", [grade.strip().lower()]))
        if amount_min is not None:
            groups.append(("the minimum amount", "amount >= ?", [amount_min]))
        if amount_max is not None:
            groups.append(("the maximum amount", "amount <= ?", [amount_max]))
        if name_contains:
            groups.append(
                (
                    "the name match",
                    "UPPER(entity_name) LIKE ? ESCAPE '\\'",
                    [f"%{_like_literal(name_contains.strip().upper())}%"],
                )
            )
        for label, (clause, clause_params) in (
            ("the organization-type filter", _org_clause(org_value)),
            ("the record-kind restriction", _record_clause(record_value)),
        ):
            if clause:
                groups.append((label, clause, list(clause_params)))
        date_sql, date_params, order_sql = _date_clause(
            date_value, from_value, to_value
        )
        if date_sql:
            groups.append(("the date window", date_sql, list(date_params)))
    except ValueError as exc:
        return f"ERROR: {exc}.", None

    db_target = db_path or db.DEFAULT_DB_PATH
    reference_notes: list[str] = []
    if record_value or date_value in {
        DateField.AWARD_RECEIVED.value,
        DateField.OPPORTUNITY_OPEN.value,
        DateField.OPPORTUNITY_CLOSE.value,
        DateField.SOLICITATION_POSTED.value,
        DateField.RESPONSE_DUE.value,
        DateField.SPEND_START.value,
        DateField.SPEND_END.value,
    }:
        reference_notes.append(
            "Event filters use only each lead's indexed current event; historical imports "
            "or sources without that event/date evidence are excluded, so coverage may be incomplete."
        )
    reference_requested = bool(
        city.strip() or enrollment_min is not None or enrollment_max is not None
    )
    enrollment_filter_ready = False
    city_filter_ready = False
    if reference_requested:
        if not state.strip():
            reference_notes.append(
                "NCES city/enrollment matching requires a two-letter state; those "
                "filters were not applied, but the other filters were."
            )
        else:
            writable = db.connect(db_target)
            try:
                school_sql, school_params = _school_name_clause()
                school_scope = (
                    "(LOWER(COALESCE(entity_type,'')) IN "
                    "('school','district','school_district','nonpublic_school') OR "
                    f"{school_sql})"
                )
                total_school = int(
                    writable.execute(
                        f"SELECT COUNT(*) FROM leads WHERE UPPER(state)=? AND {school_scope}",
                        [state.strip().upper(), *school_params],
                    ).fetchone()[0]
                )
                known_enrollment = int(
                    writable.execute(
                        f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                        AND {school_scope} AND enrollment IS NOT NULL""",
                        [state.strip().upper(), *school_params],
                    ).fetchone()[0]
                )
                known_city = int(
                    writable.execute(
                        f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                        AND {school_scope} AND location_city IS NOT NULL""",
                        [state.strip().upper(), *school_params],
                    ).fetchone()[0]
                )
                needs_enrollment = (
                    enrollment_min is not None or enrollment_max is not None
                ) and known_enrollment == 0
                needs_city = bool(city.strip() and known_city == 0)
                if total_school and (needs_enrollment or needs_city):
                    (on_progress or _NOOP)("Checking NCES enrollment")
                    from ..enrich import nces

                    nces.enrich_state_leads(writable, state)
                    known_enrollment = int(
                        writable.execute(
                            f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                            AND {school_scope} AND enrollment IS NOT NULL""",
                            [state.strip().upper(), *school_params],
                        ).fetchone()[0]
                    )
                    known_city = int(
                        writable.execute(
                            f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                            AND {school_scope} AND location_city IS NOT NULL""",
                            [state.strip().upper(), *school_params],
                        ).fetchone()[0]
                    )
                enrollment_filter_ready = known_enrollment > 0
                city_filter_ready = known_city > 0
                if enrollment_min is not None or enrollment_max is not None:
                    reference_notes.append(
                        f"NCES enrollment matched {known_enrollment} of "
                        f"{total_school} indexed school entities in {state.upper()}; "
                        "unmatched entities are excluded from enrollment-filtered results."
                        if enrollment_filter_ready
                        else "NCES enrollment did not match any indexed school entities; the "
                        "enrollment filter was not applied, but the other filters were."
                    )
                if city.strip() and not city_filter_ready:
                    reference_notes.append(
                        "NCES did not provide a matched district-office city for these "
                        "leads; the city filter was not applied."
                    )
            except Exception as exc:  # noqa: BLE001 — disclose unavailable reference data
                reference_notes.append(
                    f"NCES reference data was unavailable ({type(exc).__name__}); city/"
                    "enrollment filters were not applied, but the other filters were."
                )
            finally:
                writable.close()

    if enrollment_filter_ready:
        if enrollment_min is not None:
            groups.append(("the enrollment filter", "enrollment >= ?", [enrollment_min]))
        if enrollment_max is not None:
            groups.append(("the enrollment filter", "enrollment <= ?", [enrollment_max]))
    if city_filter_ready and city.strip():
        groups.append(
            ("the city filter", "UPPER(location_city) = ?", [city.strip().upper()])
        )

    def _assemble(skip: str = "") -> tuple[str, list[object]]:
        """Flatten the filter groups into SQL, optionally dropping one label."""
        sql_parts: list[str] = []
        sql_params: list[object] = []
        for label, clause, clause_params in groups:
            if label == skip:
                continue
            sql_parts.append(clause)
            sql_params.extend(clause_params)
        return " AND ".join(sql_parts), sql_params

    reference_note = ("\n" + " ".join(reference_notes)) if reference_notes else ""
    try:
        connection = sqlite3.connect(f"file:{db_target}?mode=ro", uri=True, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")  # count + rows share one stable read snapshot
            where_sql, params = _assemble()
            total = int(
                connection.execute(
                    f"{_SEARCH_CTE} SELECT COUNT(*) FROM searchable_leads WHERE {where_sql}",
                    params,
                ).fetchone()[0]
            )
            if total == 0:
                # Guided recovery: never a bare dead end. Count what one dropped
                # filter at a time would find so the model can offer real,
                # numbered alternatives ("without the date window: 4,463").
                hints: list[str] = []
                for label, _clause, _clause_params in groups:
                    if label == "base" or any(label in hint for hint in hints):
                        continue
                    relaxed_sql, relaxed_params = _assemble(skip=label)
                    relaxed = int(
                        connection.execute(
                            f"{_SEARCH_CTE} SELECT COUNT(*) FROM searchable_leads "
                            f"WHERE {relaxed_sql}",
                            relaxed_params,
                        ).fetchone()[0]
                    )
                    if relaxed > 0:
                        hints.append(f"without {label}: {relaxed:,} matches")
                    if len(hints) >= 3:
                        break
                if not hints:
                    # Even one-filter-dropped counts were zero: report the whole
                    # searchable pool so the model can still guide, never dead-end.
                    pool = int(
                        connection.execute(
                            f"{_SEARCH_CTE} SELECT COUNT(*) FROM searchable_leads "
                            f"WHERE {groups[0][1]}",
                            [],
                        ).fetchone()[0]
                    )
                    if pool > 0:
                        hints.append(
                            f"dropping every filter: {pool:,} leads on file overall"
                        )
                hint_note = (
                    "\nNearby alternatives — " + "; ".join(hints) + ". Offer these "
                    "to the user (with counts) and ask which to run; do not stop "
                    "at a bare no-results answer."
                    if hints
                    else ""
                )
                return (
                    "No grants matched those filters." + hint_note + reference_note,
                    None,
                )
            if (
                total > 15
                and int(limit or 50) > 15
                and not export_value
                and not with_contacts
            ):
                return (
                    f"Found {total} matches. That's a large result set — would you "
                    f"like an Excel file or a Google Sheet?{reference_note}",
                    None,
                )
            requested_export_rows = (
                total
                if scope_value == ResultScope.ALL.value
                else min(total, max(1, int(limit or 50)))
            )
            if export_value and requested_export_rows > MAX_EXPORT_ROWS:
                return (
                    f"Found {total} matches, but the requested export contains "
                    f"{requested_export_rows} rows, which exceeds the "
                    f"{MAX_EXPORT_ROWS}-row "
                    "export safety limit. Refine the search; no incomplete file was "
                    "created.",
                    None,
                )

            if with_contacts:
                # Contacts are a bounded top-N feature: enrich (and show/export) only as
                # many as we'll actually look up, so no row gets a misleading blank.
                row_limit = min(total, int(limit or 50), MAX_ENRICH_ROWS)
            elif export_value:
                row_limit = requested_export_rows
            else:
                row_limit = max(1, min(int(limit or 50), MAX_INLINE_LIMIT))
            # `, id` makes the order TOTAL so a repeated search returns the SAME rows —
            # otherwise ties (e.g. many awards sharing funds_start) could enrich orgs the
            # rep never saw. id is selected for enrichment persistence, not displayed.
            select_sql = (
                f"{_SEARCH_CTE} SELECT id, {', '.join(_SEARCH_COLUMNS)} "
                "FROM searchable_leads "
                f"WHERE {where_sql} ORDER BY {order_sql}, id LIMIT ?"
            )
            rows = connection.execute(select_sql, params + [row_limit]).fetchall()
            # Grade split over the FULL match set, so the rep hears "29 gold,
            # 6 silver" up front without knowing the internal grading jargon.
            grade_counts = {
                str(grade_row[0] or "watch"): int(grade_row[1])
                for grade_row in connection.execute(
                    f"{_SEARCH_CTE} SELECT lead_grade, COUNT(*) FROM "
                    f"searchable_leads WHERE {where_sql} GROUP BY lead_grade",
                    params,
                )
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return f"ERROR: search failed ({exc}).", None

    columns = [
        "grant_lead_id",
        "record_kind",
        "entity_role",
        *_SEARCH_COLUMNS,
        "date_context",
    ]
    data_rows: list[list[object]] = [
        [
            int(row["id"]),
            _record_kind_for_row(row),
            _entity_role_for_row(row),
            *[
                _record_link(row) if column == "detail_url" else row[column]
                for column in _SEARCH_COLUMNS
            ],
            _window_label(row),
        ]
        for row in rows
    ]

    snapshot_id = ""
    if requester_slack and workspace and channel and thread_ts:
        filters = {
            "state": state,
            "org_type": org_type,
            "program": program,
            "grade": grade,
            "record_kind": record_kind,
            "amount_min": amount_min,
            "amount_max": amount_max,
            "enrollment_min": enrollment_min,
            "enrollment_max": enrollment_max,
            "city": city,
            "name_contains": name_contains,
            "date_field": date_field,
            "date_from": date_from,
            "date_to": date_to,
        }
        session_key = f"{workspace}:{channel}:{thread_ts}:{requester_slack}"
        writable = db.connect(db_target)
        try:
            snapshot_id = db.save_search_request(
                writable,
                session_key,
                requester_slack,
                filters,
                scope_value,
                None if scope_value == ResultScope.ALL.value else int(limit or 50),
                export_value or "slack",
                [int(row["id"]) for row in rows],
            )
        finally:
            writable.close()
    snapshot_note = f"\nInternal search snapshot: {snapshot_id}." if snapshot_id else ""

    # SECOND step: enrich contacts for the (bounded) shown orgs and append the columns to
    # BOTH the export and the inline summary, so the three output paths never disagree.
    contact_cells: list[list[object]] = []
    enrich_note = ""
    if with_contacts and rows:
        contact_cells, enrich_note = _enrich_contacts(
            rows, db_target, int(limit or 50), on_progress
        )
        columns = [*columns, *_CONTACT_COLUMNS]
        data_rows = [row + cells for row, cells in zip(data_rows, contact_cells)]

    exported_label = (
        f"all {len(rows)}"
        if scope_value == ResultScope.ALL.value
        else f"the top {len(rows)}"
    )
    export_job_id = ""
    if export_value and requester_slack:
        writable = db.connect(db_target)
        try:
            export_job_id = db.create_export_job(
                writable,
                requester_slack,
                export_value,
                snapshot_id or str(uuid.uuid4()),
                snapshot_id or None,
            )
        finally:
            writable.close()

    if export_value == ExportFormat.GOOGLE_SHEET.value:
        (on_progress or _NOOP)("Preparing your Google Sheet")
        # Export is Grant's own capability (its service account + shared drive), not a
        # Persequor call; the roster read still comes from the shared reps.json helper.
        from .. import google_sheets, persequor_client

        send_as = persequor_client.rep_email_for(requester_slack) or ""
        state_value, message = google_sheets.create_sheet(
            "Grant search results", columns, data_rows, requester_slack, send_as
        )
        if state_value == "created":
            if export_job_id:
                writable = db.connect(db_target)
                try:
                    external_id = message.split("/d/", 1)[-1].split("/", 1)[0]
                    db.finish_export_job(
                        writable, export_job_id, "created", message, external_id
                    )
                finally:
                    writable.close()
            return (
                f"Found {total} matches and exported {exported_label}: "
                f"{message}{enrich_note}{reference_note}{snapshot_note}"
            ), None
        text, artifact = make_spreadsheet("grant_search.xlsx", [columns] + data_rows)
        if export_job_id:
            writable = db.connect(db_target)
            try:
                db.finish_export_job(
                    writable, export_job_id, "fallback_excel", error=message
                )
            finally:
                writable.close()
        return (
            f"Found {total} matches and exported {exported_label}. {message}; "
            f"I created a complete Excel file instead. "
            f"{text}{enrich_note}{reference_note}{snapshot_note}",
            artifact,
        )
    if export_value == ExportFormat.EXCEL.value:
        text, artifact = make_spreadsheet("grant_search.xlsx", [columns] + data_rows)
        if export_job_id:
            writable = db.connect(db_target)
            try:
                db.finish_export_job(writable, export_job_id, "created")
            finally:
                writable.close()
        return (
            f"Found {total} matching grants and exported {exported_label}. "
            f"{text}{enrich_note}{reference_note}{snapshot_note}",
            artifact,
        )

    lines: list[str] = []
    for index, row in enumerate(rows[:15]):
        amount = f"${row['amount']:,.0f}" if row["amount"] is not None else "$ n/a"
        role = _entity_role_for_row(row)
        contact = (
            _contact_suffix(contact_cells[index]) if index < len(contact_cells) else ""
        )
        location = f", {row['location_city']}" if row["location_city"] else ""
        enrollment = (
            f" · {int(row['enrollment']):,} students"
            if row["enrollment"] is not None
            else ""
        )
        # Every shown row carries its public source link — the link keeps the
        # model (and the pipeline) honest; a row without one says so plainly.
        # _record_link pins the URL to THIS award when the source supports it.
        record_url = _record_link(row)
        source_link = (
            f" · <{record_url}|verify this record>"
            if record_url
            else " · no source link on file"
        )
        lines.append(
            f"- Lead #{row['id']} — {display_entity_name(row['entity_name'])} "
            f"({row['state'] or '?'}{location}, {role}) — "
            f"{row['program'] or row['lead_grade']} · {amount} · "
            f"{_window_label(row)}{enrollment}{contact}{source_link}"
        )
    shown = min(len(rows), 15)
    more = (
        f"\nShowing {shown} of {total} matches — refine the search or export all results."
        if total > shown
        else ""
    ) + enrich_note
    inference_note = (
        "\nOrganization type is conservatively inferred from the entity name "
        "when the source does not provide a structured type."
        if org_value and org_value != "any"
        else ""
    )
    # Lead with the grade split so a rep who has never heard the internal
    # gold/silver/watch jargon still gets it explained in the same breath.
    grade_phrases = {
        "gold": "gold (award won, money to spend)",
        "silver": "silver (open solicitation)",
        "watch": "watch (worth monitoring)",
    }
    split = ", ".join(
        f"{grade_counts[key]} {grade_phrases.get(key, key)}"
        for key in ("gold", "silver", "watch")
        if grade_counts.get(key)
    )
    split_note = f" — {split}" if split else ""
    return (
        f"Found {total} matching grants{split_note}:\n"
        + "\n".join(lines)
        + more
        + inference_note
        + reference_note
        + snapshot_note
    ), None
