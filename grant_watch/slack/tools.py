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
from collections.abc import Callable
from typing import Any

import requests

from .. import db

Progress = Callable[[str], None]
_NOOP: Progress = lambda _msg: None

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
        "description": "READ-ONLY check of whether an awardee already exists in "
                       "Monarch's Salesforce (Account/Lead/Opportunity), returning the "
                       "record link + owner. Matches intelligently on name variations, "
                       "and on domain/phone if you pass them. Use it before drafting "
                       "outreach so a rep doesn't contact an org a teammate owns. "
                       "Uncertain matches come back as 'possible' — say so, never "
                       "assert. Grant can NEVER change Salesforce, only read it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string"},
                "domain": {"type": "string", "description": "org website, optional"},
                "phone": {"type": "string", "description": "org phone, optional"},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "find_person_linkedin",
        "description": "Find the likely decision-maker's LinkedIn profile (name, title, "
                       "profile link) for an org — useful when the website has no email. "
                       "Returns a PERSON to reach via LinkedIn, never an invented email.",
        "input_schema": {
            "type": "object",
            "properties": {"entity": {"type": "string"}, "state": {"type": "string"}},
            "required": ["entity", "state"],
        },
    },
    {
        "name": "search_leads",
        "description": "ON-DEMAND SEARCH of the grant database by filters. Use this when "
                       "a rep asks to find/show/list grants or awardees by criteria "
                       "(state, org type, grant program, grade, funding amount, how "
                       "recently seen, how soon a window closes, name). Set export=true "
                       "to attach the results as an Excel file. If the request is "
                       "ambiguous (no state, no timeframe, unclear org type), ask a "
                       "clarifying question FIRST instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "2-letter, e.g. CA"},
                "org_type": {"type": "string",
                             "enum": ["school", "city", "county", "hospital", "any"]},
                "program": {"type": "string",
                            "description": "grant type: SVPP, NSGP, CSSGP, STOP, ..."},
                "grade": {"type": "string", "enum": ["gold", "silver", "watch"]},
                "amount_min": {"type": "number"},
                "amount_max": {"type": "number"},
                "seen_within_days": {"type": "integer",
                                     "description": "first seen within N days (recency)"},
                "closing_within_days": {"type": "integer",
                                        "description": "spend/close window ends within N days"},
                "name_contains": {"type": "string"},
                "limit": {"type": "integer", "description": "default 50"},
                "export": {"type": "boolean", "description": "attach results as .xlsx"},
            },
            "required": [],
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


def web_search(query: str, on_progress: Progress | None = None) -> str:
    """Firecrawl search -> compact 'title — url — snippet' lines (max 5)."""
    (on_progress or _NOOP)("Searching the web")
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


# Org-type -> name patterns (entity_type is sparsely populated, but names are reliable:
# "... SCHOOL DISTRICT", "CITY OF ...", "... COUNTY", "... HOSPITAL").
_ORG_TYPE_PATTERNS: dict[str, list[str]] = {
    "school": ["%SCHOOL%", "%DISTRICT%", "%ACADEMY%", "%CHARTER%", "%ISD%", "%USD%"],
    "city": ["%CITY%", "%TOWN%", "%BOROUGH%", "%TOWNSHIP%", "%VILLAGE%", "%MUNICIP%"],
    "county": ["%COUNTY%"],
    "hospital": ["%HOSPITAL%", "%HEALTH%", "%MEDICAL%", "%CLINIC%"],
}
_SEARCH_COLS = ("entity_name", "state", "program", "amount", "lead_grade",
                "funds_start", "funds_end", "status", "detail_url")


def search_leads(state: str = "", org_type: str = "", program: str = "",
                 grade: str = "", amount_min: float | None = None,
                 amount_max: float | None = None,
                 seen_within_days: int | None = None,
                 closing_within_days: int | None = None, name_contains: str = "",
                 limit: int = 50, export: bool = False,
                 on_progress: Progress | None = None,
                 db_path: Any = None) -> tuple[str, str | None]:
    """Filtered, read-only search over the leads DB. Returns (summary_text, xlsx_path
    or None). Every filter is parameterized — no SQL injection surface. Dead leads are
    excluded. db_path is injectable for tests; defaults to the live DB."""
    (on_progress or _NOOP)("Searching the grants")
    where: list[str] = ["status != 'dead'"]
    params: list[Any] = []
    if state:
        where.append("UPPER(state) = ?"); params.append(state.strip().upper())
    if program:
        where.append("UPPER(program) LIKE ?"); params.append(f"%{program.strip().upper()}%")
    if grade:
        where.append("lead_grade = ?"); params.append(grade.strip().lower())
    if amount_min is not None:
        where.append("amount >= ?"); params.append(amount_min)
    if amount_max is not None:
        where.append("amount <= ?"); params.append(amount_max)
    if name_contains:
        where.append("UPPER(entity_name) LIKE ?"); params.append(f"%{name_contains.strip().upper()}%")
    if seen_within_days:
        where.append("first_seen >= datetime('now', ?)"); params.append(f"-{int(seen_within_days)} days")
    if closing_within_days:
        where.append("funds_end IS NOT NULL AND date(funds_end) "
                     "BETWEEN date('now') AND date('now', ?)")
        params.append(f"+{int(closing_within_days)} days")
    patterns = _ORG_TYPE_PATTERNS.get((org_type or "").lower())
    if patterns:
        where.append("(" + " OR ".join(["UPPER(entity_name) LIKE ?"] * len(patterns)) + ")")
        params.extend(patterns)

    sql = (f"SELECT {', '.join(_SEARCH_COLS)} FROM leads WHERE {' AND '.join(where)} "
           f"ORDER BY (amount IS NULL), amount DESC, funds_end ASC LIMIT ?")
    params.append(max(1, min(int(limit or 50), 1000)))
    try:
        conn = sqlite3.connect(f"file:{db_path or db.DEFAULT_DB_PATH}?mode=ro",
                               uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        return f"ERROR: search failed ({exc}).", None
    if not rows:
        return "No grants matched those filters.", None

    if export:
        header = [list(_SEARCH_COLS)]
        data = header + [[r[c] for c in _SEARCH_COLS] for r in rows]
        text, path = make_spreadsheet("grant_search.xlsx", data)
        return f"Found {len(rows)} matching grants — {text}", path

    # inline summary: compact lines, capped so Slack stays readable
    lines = []
    for r in rows[:15]:
        amt = f"${r['amount']:,.0f}" if r["amount"] else "$ n/a"
        lines.append(f"- {r['entity_name'].title()} ({r['state'] or '?'}) — "
                     f"{r['program'] or r['lead_grade']} · {amt}"
                     f"{' · closes ' + r['funds_end'] if r['funds_end'] else ''}")
    more = f"\n(+{len(rows) - 15} more — say 'export' for the full list as Excel)" \
        if len(rows) > 15 else ""
    return f"Found {len(rows)} matching grants:\n" + "\n".join(lines) + more, None


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


def find_contact(lead_id: int, entity: str, state: str,
                 on_progress: Progress | None = None) -> str:
    """Run enrichment for a lead and persist the outcome (verified or not_found)."""
    from ..enrich import finder  # local import: keeps poll/digest paths light

    conn = db.connect()
    existing = [c for c in db.contacts_for_lead(conn, lead_id)
                if c["contact_status"] == "verified"]
    if existing:
        c = existing[0]
        return (f"Already on file: {c['name']} ({c['title']}) — {c['email']} "
                f"(source: {c['source_url']})")
    candidate = finder.find_contact(entity, state, on_progress=on_progress)
    if candidate is None:
        db.mark_contact_not_found(conn, lead_id)
        return ("No verifiable contact found on their website (email must appear on a "
                "page we actually fetched). Recorded as not_found — try "
                "find_person_linkedin for a name, or a human can supply one.")
    db.save_contact(conn, lead_id, candidate.name, candidate.title, candidate.email,
                    candidate.phone, candidate.source_url, candidate.confidence)
    return (f"VERIFIED contact: {candidate.name} ({candidate.title}) — "
            f"{candidate.email}{' / ' + candidate.phone if candidate.phone else ''} "
            f"(found on {candidate.source_url}, confidence {candidate.confidence})")


def salesforce_lookup(entity: str, domain: str = "", phone: str = "",
                      on_progress: Progress | None = None) -> str:
    """Read-only CRM cross-reference — honest, link-carrying summary for Grant."""
    from ..enrich import salesforce

    res = salesforce.lookup(entity, domain=domain, phone=phone, on_progress=on_progress)
    if res.error:
        return f"ERROR: {res.error} — tell the user you couldn't reach Salesforce."
    if not res.matched:
        return f"No Salesforce record found for '{entity}' — looks net-new."
    lines = []
    for m in res.matches[:6]:
        tag = "match" if m.confidence == "high" else "possible match"
        who = m.company or m.name
        owner = f", owned by {m.owner}" if m.owner else ""
        lines.append(f"- {m.sobject} ({tag}): {who}{owner} -> {m.link}")
    extra = (f"\n(+{len(res.matches) - 6} more — worth reviewing)"
             if len(res.matches) > 6 else "")
    header = ("One Salesforce result:" if len(res.matches) == 1
              else f"{len(res.matches)} Salesforce results (review before outreach):")
    return header + "\n" + "\n".join(lines) + extra


def find_person_linkedin(entity: str, state: str,
                         on_progress: Progress | None = None) -> str:
    """LinkedIn profile of the likely decision-maker (name/title/link, no email)."""
    from ..enrich import finder

    person = finder.linkedin_person(entity, state, on_progress=on_progress)
    if person is None:
        return "No clear LinkedIn profile found for their decision-maker."
    role = f", {person['title']}" if person["title"] else ""
    return (f"LinkedIn: {person['name']}{role} — {person['url']} "
            f"(reach out via LinkedIn; no email verified)")


def run_tool(name: str, args: dict[str, Any],
             on_progress: Progress | None = None) -> tuple[str, str | None]:
    """Dispatch one tool call. Returns (result_text_for_model, file_path_or_None).
    on_progress emits short status phrases for Grant's live spinner."""
    p = on_progress or _NOOP
    if name == "web_search":
        return web_search(str(args.get("query", "")), p), None
    if name == "salesforce_lookup":
        try:
            return salesforce_lookup(str(args.get("entity", "")),
                                     str(args.get("domain", "")),
                                     str(args.get("phone", "")), p), None
        except Exception as exc:
            return f"ERROR: Salesforce lookup failed ({type(exc).__name__}).", None
    if name == "query_leads":
        p("Checking the lead database")
        return query_leads(str(args.get("sql", ""))), None
    if name == "search_leads":
        try:
            return search_leads(
                state=str(args.get("state", "")),
                org_type=str(args.get("org_type", "")),
                program=str(args.get("program", "")),
                grade=str(args.get("grade", "")),
                amount_min=args.get("amount_min"),
                amount_max=args.get("amount_max"),
                seen_within_days=args.get("seen_within_days"),
                closing_within_days=args.get("closing_within_days"),
                name_contains=str(args.get("name_contains", "")),
                limit=int(args.get("limit", 50) or 50),
                export=bool(args.get("export", False)),
                on_progress=p)
        except Exception as exc:
            return f"ERROR: search failed ({type(exc).__name__}).", None
    if name == "find_contact":
        try:
            return find_contact(int(args.get("lead_id", 0)),
                                str(args.get("entity", "")),
                                str(args.get("state", "")), p), None
        except Exception as exc:  # enrichment API hiccup -> honest tool error
            return f"ERROR: enrichment failed ({type(exc).__name__}) — say so.", None
    if name == "find_person_linkedin":
        try:
            return find_person_linkedin(str(args.get("entity", "")),
                                        str(args.get("state", "")), p), None
        except Exception as exc:
            return f"ERROR: LinkedIn search failed ({type(exc).__name__}).", None
    if name == "make_spreadsheet":
        p("Building the spreadsheet")
        return make_spreadsheet(str(args.get("filename", "")),
                                list(args.get("rows", [])))
    return f"ERROR: unknown tool {name}", None
