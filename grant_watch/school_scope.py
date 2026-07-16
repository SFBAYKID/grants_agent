"""Shared conservative SQL classification for school-like grant recipients."""

from __future__ import annotations

SCHOOL_ENTITY_TYPES = ("school", "district", "school_district", "nonpublic_school")
SCHOOL_NAME_PATTERNS = (
    "%SCHOOL%", "%ACADEMY%", "%CHARTER%", "% ISD", "% ISD %",
    "% USD", "% USD %", "%SCHOOL DISTRICT%",
)


def school_name_clause() -> tuple[str, list[object]]:
    """Return the fixed name-fallback predicate and its bound parameters."""
    clause = "(" + " OR ".join(
        "UPPER(entity_name) LIKE ?" for _ in SCHOOL_NAME_PATTERNS) + ")"
    return clause, list(SCHOOL_NAME_PATTERNS)


def school_entity_clause() -> tuple[str, list[object]]:
    """Return one shared stored-type-or-name school predicate."""
    stored = "LOWER(COALESCE(entity_type, '')) IN (" + ",".join(
        "?" for _ in SCHOOL_ENTITY_TYPES) + ")"
    names, name_params = school_name_clause()
    return f"({stored} OR {names})", [*SCHOOL_ENTITY_TYPES, *name_params]
