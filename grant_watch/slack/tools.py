"""Grant's server-side tools — the hands behind the conversation (Chase's ask:
"if I ask for an Excel, Grant calls a tool and puts the data back in the thread").

Three tools, each honest by construction:
  web_search        real results from Firecrawl's search API; links are returned
                    verbatim, never invented. No key or an API error -> says so.
  query_leads       read-only SELECT against Grant's own SQLite (opened mode=ro,
                    SELECT-only enforced) — Grant can answer data questions with
                    real numbers instead of vibes.
  make_spreadsheet  builds a real .xlsx (openpyxl); grant.py uploads it to the
                    thread and deletes the temp file.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from typing import Any

import requests

from .. import db

# Tool schemas passed to the Anthropic API (the model picks; we execute).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": "Search the public web. Returns real titles, URLs and "
                       "snippets. Use for news/articles about districts, grant "
                       "programs, deadlines. Never invent links — only cite what "
                       "this returns.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "query_leads",
        "description": "Run a read-only SQL SELECT on Grant's lead database. "
                       "Tables: leads(id, source, source_item_id, lead_grade, "
                       "entity_name, title, state, program, amount, funds_start, "
                       "funds_end, detail_url, status, status_note, assigned_to), "
                       "posts, engagement, runs. Use for any question about leads, "
                       "counts, amounts, states, statuses.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string",
                                   "description": "A single SELECT statement"}},
            "required": ["sql"],
        },
    },
    {
        "name": "make_spreadsheet",
        "description": "Create a real .xlsx file that will be uploaded into this "
                       "Slack thread. rows[0] is the header row. Use when the rep "
                       "asks for a spreadsheet/export/Excel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "e.g. wa_leads.xlsx"},
                "rows": {"type": "array", "items": {"type": "array",
                                                    "items": {"type": ["string", "number", "null"]}}},
            },
            "required": ["filename", "rows"],
        },
    },
]

_SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE)


def web_search(query: str) -> str:
    """Firecrawl search -> compact 'title — url — snippet' lines (max 5)."""
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        return "ERROR: no search key configured — say you can't search right now."
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "limit": 5},
            timeout=25,
        )
        resp.raise_for_status()
        results = resp.json().get("data", [])
    except Exception as exc:
        return f"ERROR: search failed ({type(exc).__name__}) — say so honestly."
    if not results:
        return "No results found."
    lines = []
    for r in results[:5]:
        lines.append(f"- {r.get('title', '(untitled)')} — {r.get('url', '')} — "
                     f"{(r.get('description') or '')[:160]}")
    return "\n".join(lines)


def query_leads(sql: str) -> str:
    """Read-only SELECT against the lead DB. Returns up to 50 rows as text."""
    if not _SELECT_ONLY.match(sql) or ";" in sql.rstrip().rstrip(";"):
        return "ERROR: only a single SELECT statement is allowed."
    uri = f"file:{db.DEFAULT_DB_PATH}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchmany(50)
    except sqlite3.Error as exc:
        return f"ERROR: {exc}"
    if not rows:
        return "(no rows)"
    cols = rows[0].keys()
    out = [" | ".join(cols)]
    out += [" | ".join(str(r[c]) for c in cols) for r in rows]
    return "\n".join(out)


def make_spreadsheet(filename: str, rows: list[list[Any]]) -> tuple[str, str]:
    """Build the .xlsx in a temp dir. Returns (tool_result_text, file_path)."""
    from openpyxl import Workbook

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "grant_export.xlsx")
    if not safe.endswith(".xlsx"):
        safe += ".xlsx"
    wb = Workbook()
    ws = wb.active
    for row in rows[:5000]:
        ws.append(list(row))
    path = os.path.join(tempfile.mkdtemp(prefix="grant_xlsx_"), safe)
    wb.save(path)
    return f"Spreadsheet created ({len(rows)} rows). It will be attached to your reply.", path


def run_tool(name: str, args: dict[str, Any]) -> tuple[str, str | None]:
    """Dispatch one tool call. Returns (result_text_for_model, file_path_or_None)."""
    if name == "web_search":
        return web_search(str(args.get("query", ""))), None
    if name == "query_leads":
        return query_leads(str(args.get("sql", ""))), None
    if name == "make_spreadsheet":
        return make_spreadsheet(str(args.get("filename", "")),
                                list(args.get("rows", [])))
    return f"ERROR: unknown tool {name}", None
