"""Grant's tool layer: safety guards and real outputs (all offline)."""

from __future__ import annotations

from openpyxl import load_workbook

from grant_watch.slack import tools


# ------------------------------------------------------------------ query_leads
def test_query_leads_rejects_non_select() -> None:
    assert tools.query_leads("DELETE FROM leads").startswith("ERROR")
    assert tools.query_leads("update leads set status='x'").startswith("ERROR")
    assert tools.query_leads("SELECT 1; DROP TABLE leads").startswith("ERROR")


def test_query_leads_runs_select() -> None:
    out = tools.query_leads("SELECT COUNT(*) AS n FROM leads")
    assert out.startswith("n") and not out.startswith("ERROR")


# ------------------------------------------------------------------ spreadsheet
def test_make_spreadsheet_builds_real_xlsx() -> None:
    """The spreadsheet helper creates a readable workbook with numeric cells intact."""
    text, artifact = tools.make_spreadsheet("test export.xlsx",
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
    _, artifact = tools.make_spreadsheet("../..//evil name!!", [["a"]])
    name = artifact.path.name
    assert "/" not in name and name.endswith(".xlsx")
    artifact.cleanup()


def test_make_spreadsheet_neutralizes_formulas_but_keeps_negative_numbers() -> None:
    """External formula-like strings become text while genuine numbers remain numeric."""
    _, artifact = tools.make_spreadsheet(
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
