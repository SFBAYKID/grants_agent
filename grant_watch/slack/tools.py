"""Grant's server-side tools — the hands behind the conversation (Chase's ask:
"if I ask for an Excel, Grant calls a tool and puts the data back in the thread").

Core tools are honest by construction:
  web_search        real results from Firecrawl's search API; links are returned
                    verbatim, never invented. No key or an API error -> says so.
  query_leads       read-only SELECT against Grant's own SQLite (opened mode=ro,
                    SELECT-only enforced) — Grant can answer data questions with
                    real numbers instead of vibes.
  search_leads      typed source-aware filters with complete, formula-safe exports.
  make_spreadsheet  builds a real .xlsx (openpyxl); grant.py uploads it to the
                    thread and deletes the temp file.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from typing import Any

import requests

from .. import db
from ..spreadsheets import GeneratedArtifact, make_spreadsheet
from .search import search_leads

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
        "description": "Read-only search of Grant's indexed database. Date meanings are "
                       "strict: discovered is Grant's import date; opportunity_open/close "
                       "is Grants.gov; solicitation_posted/response_due is an RFP; "
                       "spend_start/end is a GOLD award's spend window. Award received "
                       "dates are not stored and must never be inferred.",
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "2-letter, e.g. CA"},
                "org_type": {"type": "string",
                             "enum": ["school", "city", "county", "hospital", "any"]},
                "program": {"type": "string",
                            "description": "grant type: SVPP, NSGP, CSSGP, STOP, ..."},
                "grade": {"type": "string", "enum": ["gold", "silver", "watch"]},
                "record_kind": {"type": "string",
                                "enum": ["award", "funding_opportunity", "solicitation"]},
                "amount_min": {"type": "number"},
                "amount_max": {"type": "number"},
                "name_contains": {"type": "string"},
                "date_field": {"type": "string",
                               "enum": ["discovered", "opportunity_open",
                                        "opportunity_close", "solicitation_posted",
                                        "response_due", "spend_start", "spend_end",
                                        "award_received"],
                               "description": "Meaning of date_from/date_to. "
                                              "award_received returns an honest unsupported error."},
                "date_from": {"type": "string", "description": "inclusive YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "inclusive YYYY-MM-DD"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "description": "inline result limit; exports ignore it"},
                "export": {"type": "string", "enum": ["excel", "google_sheet"],
                           "description": "export every match or refuse above the declared cap"},
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
             on_progress: Progress | None = None,
             requester_slack: str = "") -> tuple[str, GeneratedArtifact | None]:
    """Dispatch one tool call and return text plus an optional owned artifact.

    on_progress emits short status phrases for Grant's live spinner; requester_slack
    is the rep asking (needed for a Google Sheet in their own Google account)."""
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
                record_kind=str(args.get("record_kind", "")),
                amount_min=(float(args["amount_min"]) if args.get("amount_min") is not None
                            else None),
                amount_max=(float(args["amount_max"]) if args.get("amount_max") is not None
                            else None),
                name_contains=str(args.get("name_contains", "")),
                date_field=str(args.get("date_field", "")),
                date_from=str(args.get("date_from", "")),
                date_to=str(args.get("date_to", "")),
                limit=int(args.get("limit", 50) or 50),
                export=args.get("export", ""),
                on_progress=p, requester_slack=requester_slack)
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
