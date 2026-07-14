"""Typed, read-only on-demand search with honest source-specific date semantics.

Why: the canonical database reuses funds_start/funds_end for award spend windows,
Grants.gov application windows, and solicitation response windows. This module keeps
those meanings separate so Grant never calls an import date an award date or a spend
deadline an application close date.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from enum import Enum
from pathlib import Path

from .. import db
from ..spreadsheets import GeneratedArtifact, make_spreadsheet

Progress = Callable[[str], None]
_NOOP: Progress = lambda _message: None

MAX_INLINE_LIMIT = 100
MAX_EXPORT_ROWS = 5_000

_SEARCH_COLUMNS = (
    "source", "source_item_id", "entity_name", "title", "entity_type", "state",
    "county", "program", "amount", "lead_grade", "funds_start", "funds_end",
    "first_seen", "last_seen", "status", "detail_url",
)


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
    """Map record-kind vocabulary to source-backed database predicates."""
    if not record_kind:
        return "", []
    mapping = {
        RecordKind.AWARD.value: "lead_grade = 'gold'",
        RecordKind.FUNDING_OPPORTUNITY.value: "source = 'grants.gov'",
        RecordKind.SOLICITATION.value: "lead_grade = 'silver'",
    }
    if record_kind not in mapping:
        raise ValueError(f"unknown record kind '{record_kind}'")
    return mapping[record_kind], []


def _date_clause(date_field: str, date_from: str,
                 date_to: str) -> tuple[str, list[object], str]:
    """Map a validated date meaning to one source-aware SQL predicate and sort order."""
    if not date_field and not date_from and not date_to:
        return "", [], "datetime(first_seen) DESC, amount DESC"
    if not date_field:
        raise ValueError("date_field is required with date_from/date_to")
    if date_field == DateField.AWARD_RECEIVED.value:
        raise ValueError("award received/announcement date is not stored; use discovered "
                         "or spend_start only if that is what you mean")
    if not date_from and not date_to:
        raise ValueError("date_from or date_to is required with date_field")
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from cannot be after date_to")

    field_map = {
        DateField.DISCOVERED.value: ("date(first_seen)", "1=1", "date(first_seen) DESC"),
        DateField.OPPORTUNITY_OPEN.value: (
            "date(funds_start)", "source = 'grants.gov'", "date(funds_start) ASC"),
        DateField.OPPORTUNITY_CLOSE.value: (
            "date(funds_end)", "source = 'grants.gov'", "date(funds_end) ASC"),
        DateField.SOLICITATION_POSTED.value: (
            "date(funds_start)", "lead_grade = 'silver'", "date(funds_start) DESC"),
        DateField.RESPONSE_DUE.value: (
            "date(funds_end)", "lead_grade = 'silver'", "date(funds_end) ASC"),
        DateField.SPEND_START.value: (
            "date(funds_start)", "lead_grade = 'gold'", "date(funds_start) DESC"),
        DateField.SPEND_END.value: (
            "date(funds_end)", "lead_grade = 'gold'", "date(funds_end) ASC"),
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
    if row["lead_grade"] == "gold":
        return f"spend window {start} through {end}"
    if row["source"] == "grants.gov":
        return f"applications open {start}; close {end}"
    if row["lead_grade"] == "silver":
        return f"posted {start}; response due {end}"
    return f"recorded window {start} through {end}"


def _record_kind_for_row(row: sqlite3.Row) -> str:
    """Derive the display/export record kind from canonical source and grade fields."""
    if row["lead_grade"] == "gold":
        return RecordKind.AWARD.value
    if row["source"] == "grants.gov":
        return RecordKind.FUNDING_OPPORTUNITY.value
    if row["lead_grade"] == "silver":
        return RecordKind.SOLICITATION.value
    return "watch"


def _entity_role_for_row(row: sqlite3.Row) -> str:
    """Distinguish a funding/posting agency from an actual award recipient."""
    if row["source"] == "grants.gov":
        return "funding agency"
    if row["lead_grade"] == "silver":
        return "posting organization"
    return "award recipient"


def _export_kind(raw: str | bool) -> str:
    """Normalize supported export values while retaining legacy True as Excel."""
    if raw is True:
        return ExportFormat.EXCEL.value
    if raw is False or raw == "":
        return ""
    return _enum_value(ExportFormat, str(raw), "export format")


def search_leads(state: str = "", org_type: str = "", program: str = "",
                 grade: str = "", record_kind: str = "",
                 amount_min: float | None = None, amount_max: float | None = None,
                 name_contains: str = "", date_field: str = "", date_from: str = "",
                 date_to: str = "", limit: int = 50, export: str | bool = "",
                 on_progress: Progress | None = None, requester_slack: str = "",
                 db_path: Path | str | None = None) -> tuple[str, GeneratedArtifact | None]:
    """Search one read-only SQLite snapshot and optionally export every matching row."""
    (on_progress or _NOOP)("Searching the grants")
    try:
        org_value = _enum_value(OrgType, org_type, "organization type")
        record_value = _enum_value(RecordKind, record_kind, "record kind")
        date_value = _enum_value(DateField, date_field, "date field")
        export_value = _export_kind(export)
        from_value = _iso_date(date_from, "date_from")
        to_value = _iso_date(date_to, "date_to")
        if grade and grade.lower() not in {"gold", "silver", "watch"}:
            raise ValueError(f"unknown grade '{grade}'")
        if amount_min is not None and amount_max is not None and amount_min > amount_max:
            raise ValueError("amount_min cannot exceed amount_max")
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
    try:
        connection = sqlite3.connect(f"file:{db_target}?mode=ro", uri=True, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")  # count + rows share one stable read snapshot
            where_sql = " AND ".join(where)
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM leads WHERE {where_sql}", params).fetchone()[0])
            if total == 0:
                return "No grants matched those filters.", None
            if export_value and total > MAX_EXPORT_ROWS:
                return (f"Found {total} matches, which exceeds the {MAX_EXPORT_ROWS}-row "
                        "export safety limit. Refine the search; no incomplete file was "
                        "created.", None)

            row_limit = (total if export_value
                         else max(1, min(int(limit or 50), MAX_INLINE_LIMIT)))
            select_sql = (f"SELECT {', '.join(_SEARCH_COLUMNS)} FROM leads "
                          f"WHERE {where_sql} ORDER BY {order_sql} LIMIT ?")
            rows = connection.execute(select_sql, params + [row_limit]).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return f"ERROR: search failed ({exc}).", None

    columns = ["record_kind", "entity_role", *_SEARCH_COLUMNS, "date_context"]
    data_rows: list[list[object]] = [
        [_record_kind_for_row(row), _entity_role_for_row(row),
         *[row[column] for column in _SEARCH_COLUMNS], _window_label(row)]
        for row in rows
    ]
    if export_value == ExportFormat.GOOGLE_SHEET.value:
        (on_progress or _NOOP)("Creating a Google Sheet")
        from .. import persequor_client

        send_as = persequor_client.rep_email_for(requester_slack) or ""
        state_value, message = persequor_client.create_google_sheet(
            "Grant search results", columns, data_rows, requester_slack, send_as)
        if state_value == "created":
            return f"Found and exported all {total} matches: {message}", None
        text, artifact = make_spreadsheet("grant_search.xlsx", [columns] + data_rows)
        return (f"Found all {total} matches. {message}; I created complete Excel instead. "
                f"{text}", artifact)
    if export_value == ExportFormat.EXCEL.value:
        text, artifact = make_spreadsheet("grant_search.xlsx", [columns] + data_rows)
        return f"Found and exported all {total} matching grants. {text}", artifact

    lines: list[str] = []
    for row in rows[:15]:
        amount = f"${row['amount']:,.0f}" if row["amount"] is not None else "$ n/a"
        role = _entity_role_for_row(row)
        lines.append(f"- {row['entity_name']} ({row['state'] or '?'}, {role}) — "
                     f"{row['program'] or row['lead_grade']} · {amount} · "
                     f"{_window_label(row)}")
    shown = min(len(rows), 15)
    more = (f"\nShowing {shown} of {total} matches — refine the search or export all results."
            if total > shown else "")
    inference_note = ("\nOrganization type is conservatively inferred from the entity name "
                      "when the source does not provide a structured type."
                      if org_value and org_value != "any" else "")
    return f"Found {total} matching grants:\n" + "\n".join(lines) + more + inference_note, None
