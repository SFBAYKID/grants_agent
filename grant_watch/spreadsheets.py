"""Safe spreadsheet artifacts shared by Slack exports and Persequor handoffs.

Why: grant data comes from external systems. Strings that begin with spreadsheet
formula markers must remain literal text, and every temporary workbook needs an
explicit owner that can clean it up on success or failure.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from openpyxl import Workbook

SpreadsheetValue: TypeAlias = str | int | float | bool | None

_FORMULA_PREFIXES = ("=", "+", "-", "@")


@dataclass(frozen=True)
class GeneratedArtifact:
    """A generated local file whose creator remains responsible for cleanup."""

    path: Path

    def cleanup(self) -> None:
        """Remove the artifact and its private temporary directory when empty."""
        try:
            self.path.unlink(missing_ok=True)
            if self.path.parent.name.startswith("grant_xlsx_"):
                self.path.parent.rmdir()
        except OSError:
            # Cleanup is best-effort; a missing/locked temp file must not crash Slack.
            return


def neutralize_spreadsheet_value(value: object) -> SpreadsheetValue:
    """Preserve JSON/SQLite scalar types while neutralizing formula-like strings."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    if text.lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def neutralize_spreadsheet_rows(
    rows: list[list[object]],
) -> list[list[SpreadsheetValue]]:
    """Return rows safe for Excel and Google Sheets without changing real numbers."""
    return [[neutralize_spreadsheet_value(value) for value in row] for row in rows]


def make_spreadsheet(
    filename: str, rows: list[list[object]]
) -> tuple[str, GeneratedArtifact]:
    """Build a complete formula-safe XLSX and return its owned temporary artifact."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "grant_export.xlsx")
    if not safe_name.lower().endswith(".xlsx"):
        safe_name += ".xlsx"

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Grant results"
    for row in neutralize_spreadsheet_rows(rows):
        sheet.append(row)
    if rows:
        sheet.freeze_panes = "A2"

    directory = Path(tempfile.mkdtemp(prefix="grant_xlsx_"))
    artifact = GeneratedArtifact(directory / safe_name)
    workbook.save(artifact.path)
    data_rows = max(0, len(rows) - 1)
    return (
        f"Spreadsheet created with {data_rows} data rows; it will be attached.",
        artifact,
    )
