"""Grant's own Google Sheets export — a first-class Grant capability, not Persequor's.

Division of labor (Chase, 2026-07-14): "Email is Persequor's domain; data export is
Grant's." So Grant creates the sheet itself with a dedicated service account and hands
the rep a link — Persequor is never in this path.

How it works:
  1. The sheet is created INSIDE the "Grant Exports" shared drive. Files in a shared
     drive are owned by Monarch's Workspace, not the service account, so the service
     account's zero personal-storage quota is never touched (a service account cannot
     own files in "My Drive" — that path 403s with a quota error).
  2. Rows are written with valueInputOption=RAW. RAW is stored verbatim and NEVER
     parsed, so a cell like "=IMPORTXML(...)" can never execute — formula injection is
     structurally impossible here (no apostrophe-escaping needed, unlike Excel).
  3. The finished sheet is shared with the requesting rep's roster email, so the link
     opens straight in their own Google account.

Auth/config (both in .env, key file gitignored):
  GOOGLE_SA_KEY_PATH      service-account JSON key; the account is a Content Manager on
                          the shared drive — least privilege (create/edit/share files in
                          that one drive; it cannot manage members or delete the drive).
  GRANT_EXPORTS_DRIVE_ID  the "Grant Exports" shared drive id.

Return contract mirrors the old Persequor handoff so search.py falls back to a complete
Excel workbook on anything but success:
  ('created', url) | ('unconfigured', reason) | ('error', reason)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# The Google client libraries are optional at import time so the rest of Grant (and the
# offline test suite) never hard-depends on them; we import lazily inside the call.
_SCOPES = ("https://www.googleapis.com/auth/drive",
           "https://www.googleapis.com/auth/spreadsheets")
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_MAX_TITLE = 120


def _cell(value: object) -> str | int | float | bool:
    """Coerce one SQLite/JSON scalar to a value the Sheets API accepts.

    None becomes an empty cell; real numbers stay numeric (RAW keeps their type). No
    formula neutralization is needed because RAW input is never parsed as a formula.
    """
    if value is None:
        return ""
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _key_path() -> Path | None:
    """Resolve the service-account key path relative to the project root when relative."""
    raw = os.environ.get("GOOGLE_SA_KEY_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path if path.is_file() else None


def create_sheet(title: str, columns: list[str], rows: list[list[object]],
                 requested_by_slack: str, send_as: str) -> tuple[str, str]:
    """Create a Google Sheet of the given rows in the Grant Exports shared drive and
    share it with the requesting rep. Returns (state, message); message is the sheet URL
    only when state == 'created'. Any other state is truthful and makes search.py return
    a complete Excel workbook instead — an export is never silently dropped."""
    if not requested_by_slack or not send_as:
        return "error", "Google Sheet export needs a rep mapped in config/reps.json"

    key_path = _key_path()
    drive_id = os.environ.get("GRANT_EXPORTS_DRIVE_ID", "").strip()
    if key_path is None or not drive_id:
        return "unconfigured", "Grant's Google Sheets export isn't configured yet"

    # Import here so a missing google client library degrades to the Excel fallback
    # instead of breaking Grant's import graph.
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return "unconfigured", "Google client libraries are not installed"

    try:
        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=list(_SCOPES))
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

        # 1) Create the empty sheet inside the shared drive (Workspace-owned, no quota).
        created = drive.files().create(
            body={"name": (title or "Grant export")[:_MAX_TITLE],
                  "mimeType": _SHEET_MIME, "parents": [drive_id]},
            fields="id,webViewLink", supportsAllDrives=True).execute()
        sheet_id = created["id"]
        sheet_url = created.get("webViewLink") or (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}")

        # 2) Write header + every row in one RAW update (search already enforced the
        #    all-or-nothing row cap upstream, so this is the complete result set).
        values: list[list[Any]] = [list(columns)]
        values.extend([_cell(value) for value in row] for row in rows)
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id, range="A1", valueInputOption="RAW",
            body={"values": values}).execute()

        # 3) Freeze + bold the header row so the export is readable at a glance.
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": 0,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {
                    "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold"}},
            ]}).execute()

        # 4) Share with the requesting rep so the link opens in their own account.
        #    Best-effort: if this one call fails, the sheet still exists in the shared
        #    drive for Chase/managers to re-share, so we surface the URL rather than
        #    discarding a good export.
        try:
            drive.permissions().create(
                fileId=sheet_id, sendNotificationEmail=False, supportsAllDrives=True,
                body={"type": "user", "role": "writer",
                      "emailAddress": send_as}).execute()
        except HttpError:
            return "created", sheet_url

        return "created", sheet_url
    except HttpError as exc:
        return "error", f"Google Sheets API error (HTTP {exc.resp.status})"
    except Exception as exc:  # noqa: BLE001 — export must degrade to Excel, never crash
        return "error", f"Google Sheet export failed ({type(exc).__name__})"
