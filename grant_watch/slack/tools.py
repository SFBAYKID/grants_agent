"""Grant's server-side tools — the hands behind the conversation (Chase's ask:
"if I ask for an Excel, Grant calls a tool and puts the data back in the thread").

Core tools are honest by construction:
  web_search        real results from Firecrawl's search API; links are returned
                    verbatim, never invented. No key or an API error -> says so.
  lead_stats        typed counts over an allowlisted lead view; no model-authored SQL.
  search_leads      typed source-aware filters with complete, formula-safe exports.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # LLM tool arguments and JSON schemas are runtime-shaped.

import requests

from .. import db
from ..spreadsheets import GeneratedArtifact
from .search import search_leads

Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop


@dataclass(frozen=True)
class ContactOutcome:
    """One lead's enrichment result — the honest, structured outcome the batch search
    and the single-lead tool both consume. status is exactly one of:
      verified    — a verbatim-verified contact (name/title/email populated),
      not_found   — we read real pages and none had a verifiable contact,
      unreachable — the source was down; NOTHING was recorded, so a retry re-attempts.
    """

    status: str
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    source_url: str = ""


def enrich_lead_contact(conn: sqlite3.Connection, lead_id: int,
                        on_progress: Progress | None = None) -> ContactOutcome:
    """Find + persist ONE lead's best contact through finder's verbatim gate, reusing a
    caller-supplied connection so a batch enriches on a single handle. Idempotent: an
    existing verified contact is returned without re-scraping. A SourceUnreachable
    outage records nothing (retryable) and is NEVER written as not_found."""
    from ..enrich import finder  # local import: keeps poll and status paths light

    lead = db.get_lead(conn, lead_id)
    if lead is None:
        raise ValueError(f"unknown Grant lead id {lead_id}")
    existing = [c for c in db.contacts_for_lead(conn, lead_id)
                if c["contact_status"] == "verified"]
    if existing:
        c = existing[0]
        return ContactOutcome("verified", c["name"] or "", c["title"] or "",
                              c["email"] or "", c["phone"] or "", c["source_url"] or "")
    try:
        candidate = finder.find_contact(
            str(lead["entity_name"]), str(lead["state"] or ""),
            on_progress=on_progress)
    except finder.SourceUnreachable:
        return ContactOutcome("unreachable")  # could not look -> record nothing
    if candidate is None:
        db.mark_contact_not_found(conn, lead_id)
        return ContactOutcome("not_found")
    db.save_contact(conn, lead_id, candidate.name, candidate.title, candidate.email,
                    candidate.phone, candidate.source_url, candidate.confidence,
                    candidate.official_domain, candidate.field_evidence)
    return ContactOutcome("verified", candidate.name, candidate.title,
                          candidate.email, candidate.phone, candidate.source_url)

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
        "name": "lead_stats",
        "description": "Return real lead counts from an allowlisted view, optionally "
                       "grouped by source, state, program, grade, or status. Use for "
                       "count/summary questions; never write SQL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string",
                             "enum": ["source", "state", "program", "grade", "status"]},
                "state": {"type": "string"},
                "program": {"type": "string"},
                "grade": {"type": "string", "enum": ["gold", "silver", "watch"]},
            },
            "required": [],
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
            },
            "required": ["lead_id"],
        },
    },
    {
        "name": "salesforce_lookup",
        "description": "READ-ONLY check of whether an awardee already exists in "
                       "Monarch's Salesforce (Account/Lead/Contact/Opportunity), returning the "
                       "record link + owner. Matches intelligently on name variations, "
                       "and on domain/phone if you pass them. Use it before drafting "
                       "outreach so a rep doesn't contact an org a teammate owns. "
                       "Uncertain matches come back as 'possible' — say so, never "
                       "assert. This lookup tool never changes Salesforce.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string"},
                "state": {"type": "string", "description": "2-letter state, optional"},
                "domain": {"type": "string", "description": "org website, optional"},
                "phone": {"type": "string", "description": "org phone, optional"},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "salesforce_campaign_search",
        "description": "Read-only search for a Salesforce Campaign by name or a pasted "
                       "Campaign link. Show candidates and ask the user to confirm one. "
                       "Never auto-select a fuzzy or multiple match.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_or_link": {"type": "string"},
            },
            "required": ["name_or_link"],
        },
    },
    {
        "name": "salesforce_campaign_create_preview",
        "description": "Prepare, but DO NOT execute, an immutable preview for creating "
                       "a new Salesforce Campaign. Use only after the user explicitly "
                       "asks to create one and has supplied a name. A Slack confirmation "
                       "button performs the later write.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "campaign_type": {"type": "string", "default": "Other"},
                "status": {"type": "string", "default": "Planned"},
                "is_active": {"type": "boolean", "default": True},
                "owner_id": {"type": "string", "description": "confirmed Salesforce User ID"},
                "owner_label": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "salesforce_campaign_members_preview",
        "description": "Prepare, but DO NOT execute, an exact preview for adding a "
                       "frozen list of Grant lead IDs to a human-confirmed Campaign. "
                       "First try existing Leads/Contacts. Set allow_org_leads=true "
                       "only after the user explicitly approves creating organization-only "
                       "Leads for unmatched organizations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_link": {"type": "string"},
                "search_request_id": {"type": "string",
                                      "description": "persisted Grant search snapshot, preferred"},
                "lead_ids": {"type": "array", "items": {"type": "integer"},
                             "minItems": 1, "maxItems": 200},
                "member_links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "grant_lead_id": {"type": "integer"},
                            "salesforce_link": {"type": "string"},
                        },
                        "required": ["grant_lead_id", "salesforce_link"],
                    },
                },
                "allow_org_leads": {"type": "boolean", "default": False},
            },
            "required": ["campaign_link"],
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
                "enrollment_min": {"type": "integer", "minimum": 0,
                                   "description": "NCES district enrollment lower bound; "
                                                  "state is required"},
                "enrollment_max": {"type": "integer", "minimum": 0,
                                   "description": "NCES district enrollment upper bound; "
                                                  "state is required"},
                "city": {"type": "string",
                         "description": "exact NCES district-office city; state is required"},
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
                          "description": "how many results the rep asked for (top N); "
                                         "with_contacts enriches this many"},
                "export": {"type": "string", "enum": ["excel", "google_sheet"],
                           "description": "export every match or refuse above the declared cap"},
                "result_scope": {"type": "string", "enum": ["top_n", "all"],
                                 "description": "top_n honors limit; all exports every match"},
                "with_contacts": {"type": "boolean",
                                  "description": "SECOND step only: after the rep says yes "
                                                 "to finding contacts, set true to enrich "
                                                 "the top `limit` orgs (~30s each) and add "
                                                 "verified-or-not-found contact columns. "
                                                 "Never set true on the first search."},
            },
            "required": [],
        },
    },
]

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


def lead_stats(group_by: str = "grade", state: str = "", program: str = "",
               grade: str = "", db_path: str | os.PathLike[str] | None = None) -> str:
    """Return typed lead counts without exposing SQL or unrelated database tables."""
    columns = {
        "source": "source", "state": "state", "program": "program",
        "grade": "lead_grade", "status": "status",
    }
    column = columns.get(group_by or "grade")
    if column is None:
        return f"ERROR: unsupported grouping '{group_by}'."
    where = ["COALESCE(status,'new') != 'dead'"]
    params: list[str] = []
    if state:
        where.append("UPPER(state)=?")
        params.append(state.strip().upper())
    if program:
        where.append("UPPER(program)=?")
        params.append(program.strip().upper())
    if grade:
        if grade.lower() not in {"gold", "silver", "watch"}:
            return f"ERROR: unsupported grade '{grade}'."
        where.append("lead_grade=?")
        params.append(grade.lower())
    target = db_path or db.DEFAULT_DB_PATH
    uri = f"file:{target}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        rows = conn.execute(
            f"SELECT COALESCE({column}, '(unknown)') AS value, COUNT(*) AS count "
            f"FROM leads WHERE {' AND '.join(where)} GROUP BY {column} "
            "ORDER BY count DESC, value LIMIT 100",
            params,
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return f"ERROR: {exc}"
    if not rows:
        return "No leads matched those filters."
    return f"Counts by {group_by or 'grade'}:\n" + "\n".join(
        f"- {value}: {count}" for value, count in rows)


def find_contact(lead_id: int, on_progress: Progress | None = None) -> str:
    """Enrich one lead and report the outcome honestly (verified / not_found /
    unreachable). Thin string wrapper over enrich_lead_contact for the single-lead tool."""
    conn = db.connect()
    outcome = enrich_lead_contact(conn, lead_id, on_progress)
    if outcome.status == "verified":
        phone = f" / {outcome.phone}" if outcome.phone else ""
        source = f" (found on {outcome.source_url})" if outcome.source_url else ""
        return f"VERIFIED contact: {outcome.name} ({outcome.title}) — {outcome.email}{phone}{source}"
    if outcome.status == "unreachable":
        return ("I couldn't reach their website or search to verify a contact right now — "
                "nothing recorded, so it's worth trying again shortly.")
    return ("No verifiable contact found on their website (email must appear on a page we "
            "actually fetched). Recorded as not_found — try find_person_linkedin for a "
            "name, or a human can supply one.")


def salesforce_lookup(entity: str, domain: str = "", phone: str = "", state: str = "",
                      on_progress: Progress | None = None) -> str:
    """Read-only CRM cross-reference — honest, link-carrying summary for Grant."""
    from ..enrich import salesforce

    res = salesforce.lookup(entity, domain=domain, phone=phone, state=state,
                            on_progress=on_progress)
    if res.error:
        return f"ERROR: {res.error} — tell the user you couldn't reach Salesforce."
    if res.status.value == "no_match":
        terms = ", ".join(res.attempted_terms) or entity
        return ("No visible Salesforce Account, Lead, or Contact match in the "
                f"connected org after a complete search for: {terms}.")
    if not res.matched:
        return "Salesforce lookup was incomplete — no net-new conclusion is safe."
    lines = []
    for m in res.matches[:6]:
        tag = "match" if m.confidence == "high" else "possible match"
        who = m.company or m.name
        owner = f", owned by {m.owner}" if m.owner else ""
        state = f", state {m.state}" if m.state else ""
        lines.append(f"- {m.sobject} ({tag}): {who}{state}{owner} -> {m.link}")
    extra = (f"\n(+{len(res.matches) - 6} more — worth reviewing)"
             if len(res.matches) > 6 else "")
    header = ("One Salesforce result:" if len(res.matches) == 1
              else f"{len(res.matches)} Salesforce results (review before outreach):")
    qualifier = ("\nSalesforce returned partial results; do not treat omissions as net-new."
                 if res.status.value == "partial" else "")
    return header + "\n" + "\n".join(lines) + extra + qualifier


def _crm_action_result(action_id: str, nonce: str, preview: str,
                       expires_at: str) -> str:
    """Append a machine-readable pending-action marker for grant.py to buttonize."""
    marker = json.dumps({
        "action_id": action_id,
        "nonce": nonce,
        "preview": preview,
        "expires_at": expires_at,
    }, separators=(",", ":"))
    return f"{preview}\n<grant-crm-action>{marker}</grant-crm-action>"


def salesforce_campaign_search(name_or_link: str) -> str:
    """Read Campaign candidates without preparing or performing a write."""
    from ..enrich import salesforce_campaigns as crm

    gateway = crm.SalesforceCampaignGateway()
    query = name_or_link.strip()
    try:
        if query.startswith(("https://", "http://")):
            _sobject, record_id = crm.parse_record_link(query, {"Campaign"})
            records = [gateway.get_record("Campaign", record_id)]
        else:
            records = gateway.search_campaigns(query)
    except (ValueError, KeyError, requests.RequestException) as exc:
        return f"ERROR: Campaign search failed ({type(exc).__name__}): {str(exc)[:160]}"
    if not records:
        return (f"No Salesforce Campaign found for '{query}'. Ask for a direct Campaign "
                "link or offer to create a new Campaign.")
    lines = [f"- {record.name} — {record.link}" for record in records]
    instruction = ("Confirm this exact Campaign with the user before preparing members."
                   if len(records) == 1 else
                   "Multiple Campaigns matched; ask the user to choose one by link.")
    return f"Found {len(records)} Campaign result(s):\n" + "\n".join(lines) + f"\n{instruction}"


def salesforce_campaign_create_preview(
        args: dict[str, Any], requester_slack: str, workspace: str,
        channel: str, thread_ts: str) -> str:
    """Persist a requester-bound Campaign creation preview and return its marker."""
    from ..enrich import salesforce_campaigns as crm

    gateway = crm.SalesforceCampaignGateway()
    owner_id = str(args.get("owner_id", ""))
    owner_label = str(args.get("owner_label", "Salesforce integration user"))
    if not owner_id:
        from .. import persequor_client

        requester_email = persequor_client.rep_email_for(requester_slack) or ""
        owners = gateway.find_active_user_by_email(requester_email)
        if len(owners) == 1:
            owner_id = owners[0].record_id
            owner_label = owners[0].name
    draft = crm.CampaignDraft(
        name=str(args.get("name", "")),
        campaign_type=str(args.get("campaign_type", "Other")),
        status=str(args.get("status", "Planned")),
        is_active=bool(args.get("is_active", True)),
        owner_id=owner_id,
        owner_label=owner_label,
        start_date=str(args.get("start_date", "")),
        end_date=str(args.get("end_date", "")),
        description=str(args.get("description", "")),
    )
    try:
        action = crm.prepare_campaign_creation(
            db.connect(), gateway, workspace, channel,
            thread_ts, requester_slack, draft,
        )
    except (ValueError, PermissionError, KeyError, requests.RequestException) as exc:
        return f"ERROR: Campaign preview failed ({type(exc).__name__}): {str(exc)[:180]}"
    return _crm_action_result(action.action_id, action.nonce,
                              action.preview, action.expires_at)


def salesforce_campaign_members_preview(
        args: dict[str, Any], requester_slack: str, workspace: str,
        channel: str, thread_ts: str) -> str:
    """Resolve and persist an exact Campaign membership preview without creating data."""
    from ..enrich import salesforce_campaigns as crm

    gateway = crm.SalesforceCampaignGateway()
    try:
        _sobject, campaign_id = crm.parse_record_link(
            str(args.get("campaign_link", "")), {"Campaign"})
        campaign = gateway.get_record("Campaign", campaign_id)
        links: dict[int, str] = {}
        for item in args.get("member_links", []) or []:
            if isinstance(item, dict):
                links[int(item.get("grant_lead_id", 0))] = str(
                    item.get("salesforce_link", ""))
        conn = db.connect()
        lead_ids = [int(item) for item in args.get("lead_ids", [])]
        snapshot_id = str(args.get("search_request_id", ""))
        if snapshot_id:
            snapshot = db.get_search_request(conn, snapshot_id, requester_slack)
            expected_session = f"{workspace}:{channel}:{thread_ts}:{requester_slack}"
            if snapshot is None or snapshot["session_key"] != expected_session:
                raise PermissionError("search snapshot is stale or belongs to another thread")
            lead_ids = [int(item) for item in json.loads(
                str(snapshot["result_lead_ids_json"]))]
        action = crm.prepare_membership(
            conn, gateway, workspace, channel, thread_ts, requester_slack,
            campaign, lead_ids,
            supplied_links=links,
            allow_org_leads=bool(args.get("allow_org_leads", False)),
        )
    except (ValueError, PermissionError, KeyError, requests.RequestException) as exc:
        return f"ERROR: Campaign member preview failed ({type(exc).__name__}): {str(exc)[:180]}"
    return _crm_action_result(action.action_id, action.nonce,
                              action.preview, action.expires_at)


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
             requester_slack: str = "", workspace: str = "", channel: str = "",
             thread_ts: str = "") -> tuple[str, GeneratedArtifact | None]:
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
                                     str(args.get("phone", "")),
                                     str(args.get("state", "")), p), None
        except Exception as exc:
            return f"ERROR: Salesforce lookup failed ({type(exc).__name__}).", None
    if name == "salesforce_campaign_search":
        p("Searching Salesforce Campaigns")
        return salesforce_campaign_search(str(args.get("name_or_link", ""))), None
    if name == "salesforce_campaign_create_preview":
        p("Preparing Campaign preview")
        return salesforce_campaign_create_preview(
            args, requester_slack, workspace, channel, thread_ts), None
    if name == "salesforce_campaign_members_preview":
        p("Resolving Campaign members")
        return salesforce_campaign_members_preview(
            args, requester_slack, workspace, channel, thread_ts), None
    if name == "lead_stats":
        p("Checking the lead database")
        return lead_stats(
            group_by=str(args.get("group_by", "grade")),
            state=str(args.get("state", "")),
            program=str(args.get("program", "")),
            grade=str(args.get("grade", ""))), None
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
                enrollment_min=(int(args["enrollment_min"])
                                if args.get("enrollment_min") is not None else None),
                enrollment_max=(int(args["enrollment_max"])
                                if args.get("enrollment_max") is not None else None),
                city=str(args.get("city", "")),
                name_contains=str(args.get("name_contains", "")),
                date_field=str(args.get("date_field", "")),
                date_from=str(args.get("date_from", "")),
                date_to=str(args.get("date_to", "")),
                limit=int(args.get("limit", 50) or 50),
                export=args.get("export", ""),
                result_scope=str(args.get("result_scope", "top_n")),
                with_contacts=bool(args.get("with_contacts", False)),
                on_progress=p, requester_slack=requester_slack,
                workspace=workspace, channel=channel, thread_ts=thread_ts)
        except Exception as exc:
            return f"ERROR: search failed ({type(exc).__name__}).", None
    if name == "find_contact":
        try:
            return find_contact(int(args.get("lead_id", 0)), p), None
        except Exception as exc:  # enrichment API hiccup -> honest tool error
            return f"ERROR: enrichment failed ({type(exc).__name__}) — say so.", None
    if name == "find_person_linkedin":
        try:
            return find_person_linkedin(str(args.get("entity", "")),
                                        str(args.get("state", "")), p), None
        except Exception as exc:
            return f"ERROR: LinkedIn search failed ({type(exc).__name__}).", None
    return f"ERROR: unknown tool {name}", None
