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
        "name": "find_contact",
        "description": "Discover WHO to contact at an awardee (Tech Director, "
                       "Superintendent, etc.): searches the entity's real website, "
                       "extracts a contact, and stores it ONLY if the email appears "
                       "verbatim on the fetched page. Slow (~30s) — tell the user "
                       "you're digging before calling it. Returns the verified "
                       "contact or an honest not-found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "integer"},
                "entity": {"type": "string"},
                "state": {"type": "string"},
            },
            "required": ["lead_id", "entity", "state"],
        },
    },
    {
        "name": "salesforce_lookup",
        "description": "Check whether an awardee already exists in Monarch's Salesforce "
                       "(Account, Lead, or Opportunity) and return the record link + "
                       "owner. Use it before drafting outreach so a rep doesn't contact "
                       "a district a teammate already owns. Uncertain name matches come "
                       "back as 'possible' — present them as such, never assert.",
        "input_schema": {
            "type": "object",
            "properties": {"entity": {"type": "string"}},
            "required": ["entity"],
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


def find_contact(lead_id: int, entity: str, state: str) -> str:
    """Run enrichment for a lead and persist the outcome (verified or not_found)."""
    from ..enrich import finder  # local import: keeps poll/digest paths light

    conn = db.connect()
    existing = [c for c in db.contacts_for_lead(conn, lead_id)
                if c["contact_status"] == "verified"]
    if existing:
        c = existing[0]
        return (f"Already on file: {c['name']} ({c['title']}) — {c['email']} "
                f"(source: {c['source_url']})")
    candidate = finder.find_contact(entity, state)
    if candidate is None:
        db.mark_contact_not_found(conn, lead_id)
        return ("No verifiable contact found (email must appear on a page we "
                "actually fetched). Recorded as not_found — a human can supply one.")
    db.save_contact(conn, lead_id, candidate.name, candidate.title, candidate.email,
                    candidate.phone, candidate.source_url, candidate.confidence)
    return (f"VERIFIED contact: {candidate.name} ({candidate.title}) — "
            f"{candidate.email}{' / ' + candidate.phone if candidate.phone else ''} "
            f"(found on {candidate.source_url}, confidence {candidate.confidence})")


def salesforce_lookup(entity: str) -> str:
    """CRM cross-reference for one entity — honest, link-carrying summary for Grant."""
    from ..enrich import salesforce

    res = salesforce.lookup(entity)
    if res.error:
        return f"ERROR: {res.error} — tell the user you couldn't reach Salesforce."
    if not res.matched:
        return f"No Salesforce record found for '{entity}' — looks net-new."
    lines = []
    for m in res.matches[:5]:
        tag = "match" if m.confidence == "high" else "possible match"
        owner = f", owned by {m.owner}" if m.owner else ""
        lines.append(f"- {m.sobject} ({tag}): {m.name}{owner} -> {m.link}")
    return "Salesforce results:\n" + "\n".join(lines)


def run_tool(name: str, args: dict[str, Any]) -> tuple[str, str | None]:
    """Dispatch one tool call. Returns (result_text_for_model, file_path_or_None)."""
    if name == "web_search":
        return web_search(str(args.get("query", ""))), None
    if name == "salesforce_lookup":
        try:
            return salesforce_lookup(str(args.get("entity", ""))), None
        except Exception as exc:
            return f"ERROR: Salesforce lookup failed ({type(exc).__name__}).", None
    if name == "query_leads":
        return query_leads(str(args.get("sql", ""))), None
    if name == "find_contact":
        try:
            return find_contact(int(args.get("lead_id", 0)),
                                str(args.get("entity", "")),
                                str(args.get("state", ""))), None
        except Exception as exc:  # enrichment API hiccup -> honest tool error
            return f"ERROR: enrichment failed ({type(exc).__name__}) — say so.", None
    if name == "make_spreadsheet":
        return make_spreadsheet(str(args.get("filename", "")),
                                list(args.get("rows", [])))
    return f"ERROR: unknown tool {name}", None
