"""Grant's tool layer: safety guards and real outputs (all offline)."""

from __future__ import annotations

from pathlib import Path

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
    text, path = tools.make_spreadsheet("test export.xlsx",
                                        [["entity", "amount"],
                                         ["Castle Rock SD", 500000]])
    assert "attached" in text
    wb = load_workbook(path)
    ws = wb.active
    assert ws["A1"].value == "entity"
    assert ws["B2"].value == 500000
    Path(path).unlink()


def test_make_spreadsheet_sanitizes_filename() -> None:
    _, path = tools.make_spreadsheet("../..//evil name!!", [["a"]])
    name = Path(path).name
    assert "/" not in name and name.endswith(".xlsx")
    Path(path).unlink()


# ------------------------------------------------------------------ dispatch
def test_run_tool_unknown_is_honest_error() -> None:
    text, path = tools.run_tool("teleport", {})
    assert text.startswith("ERROR") and path is None
