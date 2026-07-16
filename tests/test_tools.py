"""Grant's tool layer: safety guards and real outputs (all offline)."""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from grant_watch import db
from grant_watch.models import Lead, LeadGrade, RawItem
from grant_watch.slack import tools
from grant_watch.spreadsheets import make_spreadsheet


# ------------------------------------------------------------------ lead_stats
def test_lead_stats_uses_typed_allowlisted_view(tmp_path: Path) -> None:
    """Counts come from a throwaway DB without model-authored SQL or local state."""
    path = tmp_path / "stats.db"
    conn = db.connect(path)
    db.upsert_lead(conn, Lead(
        RawItem("test", "1", "award", "Alpha District", "CA", "SVPP",
                100_000, "2026-01-01", "2027-01-01", "", {}),
        LeadGrade.GOLD,
    ))
    out = tools.lead_stats(group_by="grade", db_path=path)
    assert "gold: 1" in out


def test_raw_sql_tool_is_not_exposed() -> None:
    """The conversational model has no arbitrary database query capability."""
    names = {schema["name"] for schema in tools.TOOL_SCHEMAS}
    assert "query_leads" not in names
    assert "make_spreadsheet" not in names
    assert "lead_stats" in names
    search_schema = next(schema for schema in tools.TOOL_SCHEMAS
                         if schema["name"] == "search_leads")
    properties = search_schema["input_schema"]["properties"]
    assert {"city", "enrollment_min", "enrollment_max"} <= set(properties)


def test_campaign_member_schema_requires_an_explicit_frozen_lead_list() -> None:
    """The model cannot preview Campaign membership without exact shown lead IDs."""
    schema = next(item for item in tools.TOOL_SCHEMAS
                  if item["name"] == "salesforce_campaign_members_preview")
    input_schema = schema["input_schema"]
    assert "search_request_id" not in input_schema["properties"]
    assert set(input_schema["required"]) == {"campaign_link", "lead_ids"}


# ------------------------------------------------------------------ spreadsheet
def test_make_spreadsheet_builds_real_xlsx() -> None:
    """The spreadsheet helper creates a readable workbook with numeric cells intact."""
    text, artifact = make_spreadsheet("test export.xlsx",
                                      [["entity", "amount"],
                                       ["Castle Rock SD", 500000]])
    assert "attached" in text
    wb = load_workbook(artifact.path)
    ws = wb.active
    assert ws["A1"].value == "entity"
    assert ws["B2"].value == 500000
    artifact.cleanup()


def test_make_spreadsheet_sanitizes_filename() -> None:
    """User-supplied filenames cannot escape the private temporary directory."""
    _, artifact = make_spreadsheet("../..//evil name!!", [["a"]])
    name = artifact.path.name
    assert "/" not in name and name.endswith(".xlsx")
    artifact.cleanup()


def test_make_spreadsheet_neutralizes_formulas_but_keeps_negative_numbers() -> None:
    """External formula-like strings become text while genuine numbers remain numeric."""
    _, artifact = make_spreadsheet(
        "safe.xlsx",
        [["equals", "plus", "minus", "at", "spaced", "negative"],
         ["=1+1", "+cmd", "-formula", "@name", " \t=SUM(A1:A2)", -25.0]],
    )
    sheet = load_workbook(artifact.path, data_only=False).active
    for column in ("A", "B", "C", "D", "E"):
        assert sheet[f"{column}2"].data_type == "s"
        assert str(sheet[f"{column}2"].value).startswith("'")
    assert sheet["F2"].value == -25.0
    assert sheet["F2"].data_type == "n"
    artifact.cleanup()


# ------------------------------------------------------------------ dispatch
def test_run_tool_unknown_is_honest_error() -> None:
    """Unknown tool names fail explicitly without creating an artifact."""
    text, artifact = tools.run_tool("teleport", {})
    assert text.startswith("ERROR") and artifact is None
