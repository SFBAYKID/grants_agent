"""Typed, read-only on-demand search with honest source-specific date semantics.

Why: the canonical database reuses funds_start/funds_end for award spend windows,
Grants.gov application windows, and solicitation response windows. This module keeps
those meanings separate so Grant never calls an import date an award date or a spend
deadline an application close date.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import date
from enum import Enum
from pathlib import Path

from .. import db
from ..presentation import display_entity_name
from ..spreadsheets import GeneratedArtifact, make_spreadsheet

Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop

MAX_INLINE_LIMIT = 100
MAX_EXPORT_ROWS = 5_000
MAX_ENRICH_ROWS = 10          # hard ceiling on per-search contact lookups (cost + latency)
ENRICH_TIME_BUDGET_S = 240.0  # stop enriching past this wall-clock; disclose the partial

_CONTACT_COLUMNS = ("contact_name", "contact_title", "contact_email", "contact_status")

_SEARCH_COLUMNS = (
    "source", "source_item_id", "entity_name", "title", "entity_type", "state",
    "county", "program", "amount", "lead_grade", "funds_start", "funds_end",
    "first_seen", "last_seen", "status", "detail_url", "nces_id", "enrollment",
    "location_city", "location_confidence",
    "current_event_type", "current_event_occurred_on",
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
    patterns = ("%SCHOOL%", "%ACADEMY%", "%CHARTER%", "% ISD", "% ISD %",
                "% USD", "% USD %", "%SCHOOL DISTRICT%")
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
    stored_sql = "LOWER(COALESCE(entity_type, '')) IN (" + ",".join(
        "?" for _ in stored_values) + ")"
    params: list[object] = list(stored_values)
    school_sql, school_params = _school_name_clause()

    if org_type == "school":
        return f"({stored_sql} OR {school_sql})", params + school_params

    if org_type == "city":
        city_patterns = ("CITY OF %", "% CITY", "TOWN OF %", "% TOWN",
                         "TOWNSHIP OF %", "% TOWNSHIP", "BOROUGH OF %", "% BOROUGH",
                         "VILLAGE OF %", "% VILLAGE", "MUNICIPALITY OF %")
        city_sql = "(" + " OR ".join(
            "UPPER(entity_name) LIKE ?" for _ in city_patterns) + ")"
        return (f"({stored_sql} OR ({city_sql} AND NOT {school_sql}))",
                params + list(city_patterns) + school_params)

    if org_type == "county":
        county_sql = "(UPPER(entity_name) LIKE 'COUNTY OF %' OR " \
                     "UPPER(entity_name) LIKE '% COUNTY')"
        return (f"({stored_sql} OR ({county_sql} AND NOT {school_sql}))",
                params + school_params)

    hospital_patterns = ("% HOSPITAL%", "% HEALTH SYSTEM%", "% MEDICAL CENTER%", "% CLINIC%")
    hospital_sql = "(" + " OR ".join(
        "UPPER(entity_name) LIKE ?" for _ in hospital_patterns) + ")"
    return f"({stored_sql} OR {hospital_sql})", params + list(hospital_patterns)


def _record_clause(record_kind: str) -> tuple[str, list[object]]:
    """Map record-kind vocabulary to evidence-backed current event predicates."""
    if not record_kind:
        return "", []
    mapping = {
        RecordKind.AWARD.value: (
            "current_event_type IN ('award_announced','award_obligated')"),
        RecordKind.FUNDING_OPPORTUNITY.value: (
            "current_event_type='application_window_opened'"),
        RecordKind.SOLICITATION.value: "current_event_type='rfp_posted'",
    }
    if record_kind not in mapping:
        raise ValueError(f"unknown record kind '{record_kind}'")
    return mapping[record_kind], []


def _date_clause(date_field: str, date_from: str,
                 date_to: str) -> tuple[str, list[object], str]:
    """Map a validated date meaning to one source-aware SQL predicate and sort order."""
    if not date_field and not date_from and not date_to:
        return "", [], "COALESCE(date(current_event_occurred_on),date(first_seen)) DESC, amount DESC"
    if not date_field:
        raise ValueError("date_field is required with date_from/date_to")
    if not date_from and not date_to:
        raise ValueError("date_from or date_to is required with date_field")
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from cannot be after date_to")

    field_map = {
        DateField.DISCOVERED.value: ("date(first_seen)", "1=1", "date(first_seen) DESC"),
        DateField.OPPORTUNITY_OPEN.value: (
            "date(current_event_occurred_on)",
            "current_event_type='application_window_opened'",
            "date(current_event_occurred_on) ASC"),
        DateField.OPPORTUNITY_CLOSE.value: (
            "date(funds_end)", "current_event_type='application_window_opened'",
            "date(funds_end) ASC"),
        DateField.SOLICITATION_POSTED.value: (
            "date(current_event_occurred_on)", "current_event_type='rfp_posted'",
            "date(current_event_occurred_on) DESC"),
        DateField.RESPONSE_DUE.value: (
            "date(funds_end)", "current_event_type='rfp_posted'", "date(funds_end) ASC"),
        DateField.SPEND_START.value: (
            "date(funds_start)",
            "current_event_type IN ('award_announced','award_obligated')",
            "date(funds_start) DESC"),
        DateField.SPEND_END.value: (
            "date(funds_end)",
            "current_event_type IN ('award_announced','award_obligated')",
            "date(funds_end) ASC"),
        DateField.AWARD_RECEIVED.value: (
            "date(current_event_occurred_on)",
            "current_event_type IN ('award_announced','award_obligated') "
            "AND current_event_verification_status='verified'",
            "date(current_event_occurred_on) DESC"),
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


def _window_label(row: sqlite3.Row) -> str:
    """Describe stored dates according to the row's verified record meaning."""
    start = row["funds_start"] or "?"
    end = row["funds_end"] or "?"
    event_type = str(row["current_event_type"] or "")
    event_date = str(row["current_event_occurred_on"] or "")
    status = _window_status(row)
    status_suffix = f" ({status})" if status != "unknown" else ""
    if event_type in {"award_announced", "award_obligated"}:
        prefix = f"award event {event_date}; " if event_date else ""
        return f"{prefix}spend window {start} through {end}{status_suffix}"
    if event_type == "application_window_opened":
        return f"applications open {start}; close {end}{status_suffix}"
    if event_type == "rfp_posted":
        return f"posted {event_date or start}; response due {end}{status_suffix}"
    if row["lead_grade"] == "gold":
        return f"spend window {start} through {end}"
    if row["source"] == "grants.gov":
        return f"applications open {start}; close {end}"
    if row["lead_grade"] == "silver":
        return f"posted {start}; response due {end}"
    return f"recorded window {start} through {end}"


def _window_status(row: sqlite3.Row, today: date | None = None) -> str:
    """Return active, expired, upcoming, or unknown from the stored window only."""
    today = today or date.today()
    try:
        start = date.fromisoformat(str(row["funds_start"] or "")[:10])
        end = date.fromisoformat(str(row["funds_end"] or "")[:10])
    except ValueError:
        return "unknown"
    if end < today:
        return "expired"
    if start > today:
        return "upcoming"
    return "active"


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


def _entity_role_for_row(row: sqlite3.Row) -> str:
    """Distinguish a funding/posting agency from an actual award recipient."""
    event_type = str(row["current_event_type"] or "")
    if event_type == "application_window_opened":
        return "funding agency"
    if event_type == "rfp_posted":
        return "posting organization"
    if event_type in {"award_announced", "award_obligated"}:
        return "award recipient"
    return "organization"


def _contact_suffix(cell: list[object]) -> str:
    """Render one enriched contact cell [name, title, email, status] as a short inline
    suffix for the summary — honest about not_found / unreachable, never fabricated."""
    name, title, email, status = (list(cell) + ["", "", "", ""])[:4]
    if status == "verified":
        who = f"{name} ({title})".strip()
        return f" · contact: {who} {email}".rstrip()
    if status == "not_found":
        return " · contact: none found"
    if status == "unreachable":
        return " · contact: source unreachable — retry"
    if status == "error":
        return " · contact: lookup error"
    if status:
        return f" · contact: {status}"
    return ""


def _export_kind(raw: str | bool) -> str:
    """Normalize supported export values while retaining legacy True as Excel."""
    if raw is True:
        return ExportFormat.EXCEL.value
    if raw is False or raw == "":
        return ""
    return _enum_value(ExportFormat, str(raw), "export format")


def _enrich_contacts(rows: list[sqlite3.Row], db_target: Path | str,
                     requested_limit: int,
                     on_progress: Progress | None) -> tuple[list[list[object]], str]:
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
                outcome = tools.enrich_lead_contact(
                    conn, int(row["id"]), say)
                cells.append([outcome.name, outcome.title, outcome.email, outcome.status])
            except Exception:  # noqa: BLE001 — one org's failure must not sink the batch
                cells.append(["", "", "", "error"])
    finally:
        conn.close()
    note = (f" (Contacts limited to the top {MAX_ENRICH_ROWS} to stay responsive.)"
            if requested_limit > MAX_ENRICH_ROWS else "")
    return cells, note


def search_leads(state: str = "", org_type: str = "", program: str = "",
                 grade: str = "", record_kind: str = "",
                 amount_min: float | None = None, amount_max: float | None = None,
                 enrollment_min: int | None = None,
                 enrollment_max: int | None = None, city: str = "",
                 name_contains: str = "", date_field: str = "", date_from: str = "",
                 date_to: str = "", limit: int = 50, export: str | bool = "",
                 result_scope: str = "top_n",
                 active_only: bool = False,
                 with_contacts: bool = False,
                 on_progress: Progress | None = None, requester_slack: str = "",
                 workspace: str = "", channel: str = "", thread_ts: str = "",
                 db_path: Path | str | None = None) -> tuple[str, GeneratedArtifact | None]:
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
        if amount_min is not None and amount_max is not None and amount_min > amount_max:
            raise ValueError("amount_min cannot exceed amount_max")
        if enrollment_min is not None and enrollment_min < 0:
            raise ValueError("enrollment_min cannot be negative")
        if enrollment_max is not None and enrollment_max < 0:
            raise ValueError("enrollment_max cannot be negative")
        if (enrollment_min is not None and enrollment_max is not None
                and enrollment_min > enrollment_max):
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
            raise ValueError(f"date field '{date_value}' is incompatible with record kind "
                             f"'{record_value}'")

        where = ["COALESCE(status, 'new') != 'dead'"]
        params: list[object] = []
        if state:
            where.append("UPPER(state) = ?")
            params.append(state.strip().upper())
        if program:
            where.append("UPPER(program) LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_literal(program.strip().upper())}%")
        if grade:
            where.append("lead_grade = ?")
            params.append(grade.strip().lower())
        if amount_min is not None:
            where.append("amount >= ?")
            params.append(amount_min)
        if amount_max is not None:
            where.append("amount <= ?")
            params.append(amount_max)
        if name_contains:
            where.append("UPPER(entity_name) LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_literal(name_contains.strip().upper())}%")
        if active_only:
            where.append(
                "current_event_type IN ('award_announced','award_obligated',"
                "'application_window_opened','rfp_posted') "
                "AND date(funds_end) >= date(?)")
            params.append(date.today().isoformat())

        for clause, clause_params in (_org_clause(org_value), _record_clause(record_value)):
            if clause:
                where.append(clause)
                params.extend(clause_params)
        date_sql, date_params, order_sql = _date_clause(
            date_value, from_value, to_value)
        if date_sql:
            where.append(date_sql)
            params.extend(date_params)
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
            "or sources without that event/date evidence are excluded, so coverage may be incomplete.")
    reference_requested = bool(
        city.strip() or enrollment_min is not None or enrollment_max is not None)
    enrollment_filter_ready = False
    city_filter_ready = False
    if reference_requested:
        if not state.strip():
            reference_notes.append(
                "NCES city/enrollment matching requires a two-letter state; those "
                "filters were not applied, but the other filters were.")
        else:
            writable = db.connect(db_target)
            try:
                school_sql, school_params = _school_name_clause()
                school_scope = (
                    "(LOWER(COALESCE(entity_type,'')) IN "
                    "('school','district','school_district','nonpublic_school') OR "
                    f"{school_sql})")
                total_school = int(writable.execute(
                    f"SELECT COUNT(*) FROM leads WHERE UPPER(state)=? AND {school_scope}",
                    [state.strip().upper(), *school_params],
                ).fetchone()[0])
                known_enrollment = int(writable.execute(
                    f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                        AND {school_scope} AND enrollment IS NOT NULL""",
                    [state.strip().upper(), *school_params],
                ).fetchone()[0])
                known_city = int(writable.execute(
                    f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                        AND {school_scope} AND location_city IS NOT NULL""",
                    [state.strip().upper(), *school_params],
                ).fetchone()[0])
                needs_enrollment = ((enrollment_min is not None
                                     or enrollment_max is not None)
                                    and known_enrollment == 0)
                needs_city = bool(city.strip() and known_city == 0)
                if total_school and (needs_enrollment or needs_city):
                    (on_progress or _NOOP)("Checking NCES enrollment")
                    from ..enrich import nces

                    nces.enrich_state_leads(writable, state)
                    known_enrollment = int(writable.execute(
                        f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                            AND {school_scope} AND enrollment IS NOT NULL""",
                        [state.strip().upper(), *school_params],
                    ).fetchone()[0])
                    known_city = int(writable.execute(
                        f"""SELECT COUNT(*) FROM leads WHERE UPPER(state)=?
                            AND {school_scope} AND location_city IS NOT NULL""",
                        [state.strip().upper(), *school_params],
                    ).fetchone()[0])
                enrollment_filter_ready = known_enrollment > 0
                city_filter_ready = known_city > 0
                if (enrollment_min is not None or enrollment_max is not None):
                    reference_notes.append(
                        f"NCES enrollment matched {known_enrollment} of "
                        f"{total_school} indexed school entities in {state.upper()}; "
                        "unmatched entities are excluded from enrollment-filtered results."
                        if enrollment_filter_ready else
                        "NCES enrollment did not match any indexed school entities; the "
                        "enrollment filter was not applied, but the other filters were.")
                if city.strip() and not city_filter_ready:
                    reference_notes.append(
                        "NCES did not provide a matched district-office city for these "
                        "leads; the city filter was not applied.")
            except Exception:  # noqa: BLE001 — report unavailable reference data plainly
                reference_notes.append(
                    "NCES reference data was unavailable; city/"
                    "enrollment filters were not applied, but the other filters were.")
            finally:
                writable.close()

    if enrollment_filter_ready:
        if enrollment_min is not None:
            where.append("enrollment >= ?")
            params.append(enrollment_min)
        if enrollment_max is not None:
            where.append("enrollment <= ?")
            params.append(enrollment_max)
    if city_filter_ready and city.strip():
        where.append("UPPER(location_city) = ?")
        params.append(city.strip().upper())
    reference_note = ("\n" + " ".join(reference_notes)) if reference_notes else ""
    try:
        connection = sqlite3.connect(f"file:{db_target}?mode=ro", uri=True, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")  # count + rows share one stable read snapshot
            where_sql = " AND ".join(where)
            total = int(connection.execute(
                f"{_SEARCH_CTE} SELECT COUNT(*) FROM searchable_leads WHERE {where_sql}",
                params).fetchone()[0])
            if total == 0:
                return "No grants matched those filters." + reference_note, None
            if (total > 15 and int(limit or 50) > 15
                    and not export_value and not with_contacts):
                return (f"Found {total} matches. That's a large result set — would you "
                        f"like an Excel file or a Google Sheet?{reference_note}", None)
            requested_export_rows = (
                total if scope_value == ResultScope.ALL.value
                else min(total, max(1, int(limit or 50))))
            if export_value and requested_export_rows > MAX_EXPORT_ROWS:
                return (f"Found {total} matches, but the requested export contains "
                        f"{requested_export_rows} rows, which exceeds the "
                        f"{MAX_EXPORT_ROWS}-row "
                        "export safety limit. Refine the search; no incomplete file was "
                        "created.", None)

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
            select_sql = (f"{_SEARCH_CTE} SELECT id, {', '.join(_SEARCH_COLUMNS)} "
                          "FROM searchable_leads "
                          f"WHERE {where_sql} ORDER BY {order_sql}, id LIMIT ?")
            rows = connection.execute(select_sql, params + [row_limit]).fetchall()
            complete_ids: list[int] = []
            if requester_slack and workspace and channel and thread_ts:
                id_limit = min(total, MAX_EXPORT_ROWS + 1)
                complete_ids = [int(item["id"]) for item in connection.execute(
                    f"{_SEARCH_CTE} SELECT id FROM searchable_leads "
                    f"WHERE {where_sql} ORDER BY {order_sql}, id LIMIT ?",
                    params + [id_limit],
                ).fetchall()]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return f"ERROR: search failed ({exc}).", None

    columns = ["grant_lead_id", "record_kind", "entity_role",
               *_SEARCH_COLUMNS, "date_context", "window_status"]
    data_rows: list[list[object]] = [
        [int(row["id"]), _record_kind_for_row(row), _entity_role_for_row(row),
         *[row[column] for column in _SEARCH_COLUMNS], _window_label(row),
         _window_status(row)]
        for row in rows
    ]

    snapshot_id = ""
    if requester_slack and workspace and channel and thread_ts:
        filters = {
            "state": state, "org_type": org_type, "program": program, "grade": grade,
            "record_kind": record_kind, "amount_min": amount_min,
            "amount_max": amount_max, "enrollment_min": enrollment_min,
            "enrollment_max": enrollment_max, "city": city,
            "name_contains": name_contains,
            "date_field": date_field, "date_from": date_from, "date_to": date_to,
            "active_only": active_only,
        }
        session_key = f"{workspace}:{channel}:{thread_ts}:{requester_slack}"
        writable = db.connect(db_target)
        try:
            snapshot_id = db.save_search_request(
                writable, session_key, requester_slack, filters, scope_value,
                None if scope_value == ResultScope.ALL.value else int(limit or 50),
                export_value or "slack", complete_ids, total,
                len(complete_ids) == total)
        finally:
            writable.close()
    # SECOND step: enrich contacts for the (bounded) shown orgs and append the columns to
    # BOTH the export and the inline summary, so the three output paths never disagree.
    contact_cells: list[list[object]] = []
    enrich_note = ""
    if with_contacts and rows:
        contact_cells, enrich_note = _enrich_contacts(
            rows, db_target, int(limit or 50), on_progress)
        columns = [*columns, *_CONTACT_COLUMNS]
        data_rows = [row + cells for row, cells in zip(data_rows, contact_cells)]

    exported_label = (f"all {len(rows)}" if scope_value == ResultScope.ALL.value
                      else f"the top {len(rows)}")
    export_job_id = ""
    if export_value and requester_slack:
        writable = db.connect(db_target)
        try:
            export_job_id = db.create_export_job(
                writable, requester_slack, export_value,
                snapshot_id or str(uuid.uuid4()), snapshot_id or None)
        finally:
            writable.close()

    if export_value == ExportFormat.GOOGLE_SHEET.value:
        (on_progress or _NOOP)("Preparing your Google Sheet")
        # Export is Grant's own capability (its service account + shared drive), not a
        # Persequor call; the roster read still comes from the shared reps.json helper.
        from .. import google_sheets, persequor_client

        send_as = persequor_client.rep_email_for(requester_slack) or ""
        state_value, message = google_sheets.create_sheet(
            "Grant search results", columns, data_rows, requester_slack, send_as)
        if state_value == "created":
            if export_job_id:
                writable = db.connect(db_target)
                try:
                    external_id = message.split("/d/", 1)[-1].split("/", 1)[0]
                    db.finish_export_job(
                        writable, export_job_id, "created", message, external_id)
                finally:
                    writable.close()
            return (f"Found {total} matches and exported {exported_label}: "
                    f"{message}{enrich_note}{reference_note}"), None
        text, artifact = make_spreadsheet("grant_search.xlsx", [columns] + data_rows)
        if export_job_id:
            writable = db.connect(db_target)
            try:
                db.finish_export_job(
                    writable, export_job_id, "fallback_excel", error=message)
            finally:
                writable.close()
        return (f"Found {total} matches and exported {exported_label}. {message}; "
                f"I created a complete Excel file instead. "
                f"{text}{enrich_note}{reference_note}", artifact)
    if export_value == ExportFormat.EXCEL.value:
        text, artifact = make_spreadsheet("grant_search.xlsx", [columns] + data_rows)
        if export_job_id:
            writable = db.connect(db_target)
            try:
                db.finish_export_job(writable, export_job_id, "created")
            finally:
                writable.close()
        return (f"Found {total} matching grants and exported {exported_label}. "
                f"{text}{enrich_note}{reference_note}", artifact)

    lines: list[str] = []
    for index, row in enumerate(rows[:15]):
        amount = f"${row['amount']:,.0f}" if row["amount"] is not None else "$ n/a"
        role = _entity_role_for_row(row)
        contact = _contact_suffix(contact_cells[index]) if index < len(contact_cells) else ""
        location = f", {row['location_city']}" if row["location_city"] else ""
        enrollment = (f" · {int(row['enrollment']):,} students"
                      if row["enrollment"] is not None else "")
        lines.append(f"- Lead #{row['id']} — {display_entity_name(row['entity_name'])} "
                     f"({row['state'] or '?'}{location}, {role}) — "
                     f"{row['program'] or row['lead_grade']} · {amount} · "
                     f"{_window_label(row)}{enrollment}{contact}")
    shown = min(len(rows), 15)
    more = ((f"\nShowing {shown} of {total} matches — refine the search or export all results."
             if total > shown else "") + enrich_note)
    inference_note = ("\nOrganization type is conservatively inferred from the entity name "
                      "when the source does not provide a structured type."
                      if org_value and org_value != "any" else "")
    return (f"Found {total} matching grants:\n" + "\n".join(lines) + more
            + inference_note + reference_note), None


def export_search_snapshot(requested_by: str, workspace: str, channel: str,
                           thread_ts: str, export: str,
                           request_id: str = "",
                           db_path: Path | str | None = None) -> tuple[str, GeneratedArtifact | None]:
    """Export the exact, ordered result set from a completed Slack search.

    Follow-up requests such as "put those in Excel" must not reconstruct filters or
    run a new search: either can silently change the set the user just approved. The
    snapshot is restricted to the initiating user and Slack thread.
    """
    try:
        export_value = _export_kind(export)
    except ValueError as exc:
        return f"ERROR: {exc}.", None
    if not export_value:
        return "ERROR: choose Excel or Google Sheet.", None
    if not all((requested_by, workspace, channel, thread_ts)):
        return "ERROR: a thread-bound search is required before exporting.", None

    db_target = db_path or db.DEFAULT_DB_PATH
    session_key = f"{workspace}:{channel}:{thread_ts}:{requested_by}"
    writable = db.connect(db_target)
    pending_artifact: GeneratedArtifact | None = None
    try:
        snapshot = (db.get_search_request(writable, request_id, requested_by)
                    if request_id else
                    db.latest_search_request(writable, session_key, requested_by))
        if snapshot is None or snapshot["session_key"] != session_key:
            return "ERROR: no completed search from you in this thread was found.", None
        if not bool(snapshot["result_complete"]):
            return ("ERROR: that older search did not preserve its complete result set. "
                    "Please run the search once more; no partial export was created."), None
        lead_ids = [int(value) for value in json.loads(
            str(snapshot["result_lead_ids_json"] or "[]"))]
        if len(lead_ids) != int(snapshot["total_count"] or 0):
            return "ERROR: the saved result set is incomplete; no partial export was created.", None
        if not lead_ids:
            return "ERROR: the saved search contains no results.", None

        placeholders = ",".join("?" for _ in lead_ids)
        connection = sqlite3.connect(f"file:{db_target}?mode=ro", uri=True, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            fetched = connection.execute(
                f"{_SEARCH_CTE} SELECT id, {', '.join(_SEARCH_COLUMNS)} "
                f"FROM searchable_leads WHERE id IN ({placeholders})", lead_ids,
            ).fetchall()
        finally:
            connection.close()
        by_id = {int(row["id"]): row for row in fetched}
        if any(lead_id not in by_id for lead_id in lead_ids):
            return ("ERROR: one or more saved records are no longer available; "
                    "no partial export was created."), None
        rows = [by_id[lead_id] for lead_id in lead_ids]
        columns = ["grant_lead_id", "record_kind", "entity_role",
                   *_SEARCH_COLUMNS, "date_context", "window_status"]
        data_rows = [
            [int(row["id"]), _record_kind_for_row(row), _entity_role_for_row(row),
             *[row[column] for column in _SEARCH_COLUMNS], _window_label(row),
             _window_status(row)]
            for row in rows
        ]
        job_id = db.create_export_job(
            writable, requested_by, export_value,
            f"{snapshot['id']}:{export_value}", str(snapshot["id"]))
        if export_value == ExportFormat.GOOGLE_SHEET.value:
            from .. import google_sheets, persequor_client

            send_as = persequor_client.rep_email_for(requested_by) or ""
            state_value, message = google_sheets.create_sheet(
                "Grant search results", columns, data_rows, requested_by, send_as)
            if state_value == "created":
                external_id = message.split("/d/", 1)[-1].split("/", 1)[0]
                db.finish_export_job(writable, job_id, "created", message, external_id)
                return f"Exported the same {len(rows)} results: {message}", None
            text, pending_artifact = make_spreadsheet(
                "grant_search.xlsx", [columns] + data_rows)
            db.finish_export_job(writable, job_id, "fallback_excel", error=message)
            artifact, pending_artifact = pending_artifact, None
            return (f"Google Sheets was unavailable ({message}); I created an Excel "
                    f"file with the same {len(rows)} results. {text}"), artifact

        text, pending_artifact = make_spreadsheet(
            "grant_search.xlsx", [columns] + data_rows)
        db.finish_export_job(writable, job_id, "created")
        artifact, pending_artifact = pending_artifact, None
        return f"Exported the same {len(rows)} results to Excel. {text}", artifact
    except sqlite3.IntegrityError:
        if pending_artifact is not None:
            pending_artifact.cleanup()
        return ("ERROR: that exact export was already created. Use its existing file "
                "or run a new search."), None
    except Exception:
        if pending_artifact is not None:
            pending_artifact.cleanup()
        raise
    finally:
        writable.close()
