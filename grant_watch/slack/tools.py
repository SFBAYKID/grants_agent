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
import re
import sqlite3
import sys
import traceback
from collections.abc import Callable
from typing import Any  # LLM tool arguments and JSON schemas are runtime-shaped.

import requests

from .. import db
from ..spreadsheets import GeneratedArtifact
from .contact_enrichment import (  # re-export: search.py and tests call these
    enrich_lead_contact,
)
from ..presentation import for_model, model_note
from .search import search_leads
from .salesforce_campaign_tools import (
    salesforce_campaign_batch_preview,
    salesforce_campaign_create_preview,
)
from .source_status import source_inventory_status
from .research_tools import (  # re-export: every tools.<name> call site is unchanged
    MAX_FETCH_CHARS,  # noqa: F401 — re-exported for callers and tests
    fetch_url,
    record_contact_fact,
    salesforce_campaign_status,
    zoominfo_contact_preview,
    zoominfo_enrich_contacts,
)
from .tool_schemas import TOOL_SCHEMAS as _TOOL_SCHEMAS

Progress = Callable[[str], None]


def _noop(_message: str) -> None:
    """Ignore an optional progress update."""


_NOOP: Progress = _noop

# Re-exported from tool_schemas so every `tools.TOOL_SCHEMAS` call site is
# unchanged; the schemas moved out at the 1000-line cap (rule 4).
TOOL_SCHEMAS: list[dict[str, Any]] = _TOOL_SCHEMAS


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
        lines.append(
            f"- {r.get('title', '(untitled)')} — {r.get('url', '')} — "
            f"{(r.get('description') or '')[:160]}"
        )
    return "\n".join(lines)


def lead_stats(
    group_by: str = "grade",
    state: str = "",
    program: str = "",
    grade: str = "",
    db_path: str | os.PathLike[str] | None = None,
) -> str:
    """Return typed lead counts without exposing SQL or unrelated database tables."""
    columns = {
        "source": "source",
        "state": "state",
        "program": "program",
        "grade": "lead_grade",
        "status": "status",
    }
    column = columns.get(group_by or "grade")
    if column is None:
        return f"ERROR: unsupported grouping '{group_by}'."
    where = [db.SEARCHABLE_LEAD_PREDICATE]
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
        f"- {value}: {count}" for value, count in rows
    )


def resolve_lead_by_name(
    conn: sqlite3.Connection, entity: str, state: str = ""
) -> int | str:
    """Resolve an exact organization name to one Grant lead id, or explain why not.

    Returns the id on a unique match; otherwise an honest human-readable string —
    never a guess between candidates."""
    name_key = db.canonical_entity_key(entity).partition("|")[0]
    if not name_key:
        return "ERROR: no organization name given — ask which entity the user means."
    rows = list(
        conn.execute(
            "SELECT DISTINCT id, entity_name, state FROM leads "
            "WHERE canonical_entity_key LIKE ? ORDER BY id",
            (f"{name_key}|%",),
        )
    )
    if state:
        narrowed = [r for r in rows if str(r["state"] or "").upper() == state.upper()]
        rows = narrowed or rows
    ids = {int(r["id"]) for r in rows}
    if len(ids) == 1:
        return ids.pop()
    if not rows:
        return (
            f"ERROR: no Grant lead is named {entity!r} — run a search first so the "
            "lead exists, then ask again."
        )
    listing = ", ".join(
        f"#{r['id']} {r['entity_name']} ({r['state']})" for r in rows[:5]
    )
    return (
        f"ERROR: several Grant leads match {entity!r}: {listing} — ask the user "
        "which Lead # they mean."
    )


def find_contact(
    lead_id: int,
    on_progress: Progress | None = None,
    entity: str = "",
    state: str = "",
) -> str:
    """Enrich one lead and report the outcome honestly (verified / not_found /
    unreachable). Thin string wrapper over enrich_lead_contact for the single-lead tool."""
    conn = db.connect()
    if lead_id <= 0 and entity:
        resolved = resolve_lead_by_name(conn, entity, state)
        if isinstance(resolved, str):
            return resolved
        lead_id = resolved
    from ..enrich.organization_profile import org_enrichment_summary

    outcome = enrich_lead_contact(conn, lead_id, on_progress)
    if outcome.status == "unreachable":
        return (
            "I couldn't reach their website or search to verify a contact right now — "
            "nothing recorded, so it's worth trying again shortly."
        )
    # The org profile (address / phone / general mailbox) is enriched ALONGSIDE the
    # contact, so surface it in EVERY reachable outcome — not just the verified one.
    # Live bug 2026-07-18: a LinkedIn-only city result said "no mailing address" even
    # though the address (e.g. 200 Main Street, Salmon) was already stored.
    org_line = org_enrichment_summary(conn, lead_id, on_progress)
    if outcome.status == "verified":
        phone = f" / {outcome.phone}" if outcome.phone else ""
        source = f" (found on {outcome.source_url})" if outcome.source_url else ""
        return (
            f"VERIFIED contact: {outcome.name} ({outcome.title}) — "
            f"{outcome.email}{phone}{source}.{org_line}"
        )
    # Fallback-chain outcomes: the tool already tried the site, LinkedIn, and the
    # org's general mailbox — report exactly which rungs produced something.
    title = f" ({outcome.title})" if outcome.title else ""
    if outcome.status == "linkedin_org_email":
        return (
            f"No email shown on their own site, but LinkedIn surfaced "
            f"{outcome.name}{title} — profile {outcome.source_url} (ownership not "
            f"verified, saved to the lead) — and the organization's general mailbox "
            f"is {outcome.email}, verified on their site.{org_line}"
        )
    if outcome.status == "linkedin_only":
        return (
            f"No verifiable email anywhere, but LinkedIn surfaced "
            f"{outcome.name}{title} — profile {outcome.source_url} (ownership not "
            "verified, saved to the lead), so a LinkedIn message is the honest path "
            f"to the person.{org_line}"
        )
    if outcome.status == "org_email":
        return (
            "No named person verified on their site or LinkedIn, but the "
            f"organization's general mailbox is {outcome.email}, verified on "
            f"{outcome.source_url or 'their site'}.{org_line}"
        )
    return (
        "I checked their website, LinkedIn, and looked for a general organization "
        "mailbox — none produced a verifiable contact, so I've logged this one as "
        f"no contact found. A human may have better luck by phone.{org_line}"
    )


def salesforce_lookup(
    entity: str,
    domain: str = "",
    phone: str = "",
    state: str = "",
    on_progress: Progress | None = None,
) -> str:
    """Read-only CRM cross-reference — honest, link-carrying summary for Grant."""
    from ..enrich import salesforce

    res = salesforce.lookup(
        entity, domain=domain, phone=phone, state=state, on_progress=on_progress
    )
    if res.error:
        return f"ERROR: {res.error} — tell the user you couldn't reach Salesforce."
    if res.status.value == "no_match":
        terms = ", ".join(res.attempted_terms) or entity
        return (
            "No visible Salesforce Account, Lead, or Contact match in the "
            f"connected org after a complete search for: {terms}."
        )
    if not res.matched:
        return "Salesforce lookup was incomplete — no net-new conclusion is safe."
    lines = []
    for m in res.matches[:6]:
        tag = "match" if m.confidence == "high" else "possible match"
        who = m.company or m.name
        owner = f", owned by {m.owner}" if m.owner else ""
        state = f", state {m.state}" if m.state else ""
        lines.append(f"- {m.sobject} ({tag}): {who}{state}{owner} -> {m.link}")
    extra = (
        f"\n(+{len(res.matches) - 6} more — worth reviewing)"
        if len(res.matches) > 6
        else ""
    )
    header = (
        "One Salesforce result:"
        if len(res.matches) == 1
        else f"{len(res.matches)} Salesforce results (review before outreach):"
    )
    qualifier = (
        "\nSalesforce returned partial results; do not treat omissions as net-new."
        if res.status.value == "partial"
        else ""
    )
    return header + "\n" + "\n".join(lines) + extra + qualifier


# The ONLY tools whose output may legitimately carry a <grant-crm-action> marker.
# conversation.py turns that marker into a real, primary-styled "Confirm in
# Salesforce" button in Grant's voice, and it harvests it from TOOL RESULTS — so any
# tool that returns attacker-controlled text could mint one. web_search returns page
# titles and snippets verbatim from arbitrary sites; a page titled with the marker
# produced a live approval button carrying attacker-chosen text, and because the
# marker is stripped before the model sees it, Grant could not tell anyone it
# happened. Everything outside this set is sanitized at the run_tool boundary.
_ACTION_PRODUCING_TOOLS = frozenset(
    {
        "salesforce_campaign_create_preview",
        "salesforce_campaign_members_preview",
        "salesforce_campaign_batch_preview",
        "salesforce_contact_record_preview",
    }
)

_CRM_ACTION_MARKER_RE = re.compile(
    r"<grant-crm-action>.*?</grant-crm-action>", re.DOTALL
)


def strip_action_markers(text: str) -> str:
    """Remove any CRM action marker from untrusted tool output.

    Deliberately removes rather than escapes: a marker in text Grant did not mint
    has no honest meaning, and leaving a visible fragment would only invite the
    model to narrate it to the rep as though it were real.
    """
    return _CRM_ACTION_MARKER_RE.sub("", text)


def _crm_action_result(
    action_id: str, nonce: str, preview: str, expires_at: str
) -> str:
    """Append a machine-readable pending-action marker for grant.py to buttonize."""
    marker = json.dumps(
        {
            "action_id": action_id,
            "nonce": nonce,
            "preview": preview,
            "expires_at": expires_at,
        },
        separators=(",", ":"),
    )
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
        return (
            f"No Salesforce Campaign found for '{query}'. Ask for a direct Campaign "
            "link or offer to create a new Campaign."
        )
    lines = [f"- {record.name} — {record.link}" for record in records]
    instruction = (
        "Confirm this exact Campaign with the user before preparing members."
        if len(records) == 1
        else model_note(
            "Multiple Campaigns matched; ask the user to choose one by link."
        )
    )
    return (
        f"Found {len(records)} Campaign result(s):\n"
        + "\n".join(lines)
        + f"\n{instruction}"
    )


def salesforce_campaign_members_preview(
    args: dict[str, Any],
    requester_slack: str,
    workspace: str,
    channel: str,
    thread_ts: str,
) -> str:
    """Resolve and persist an exact Campaign membership preview without creating data."""
    from ..enrich import salesforce_campaigns as crm

    gateway = crm.SalesforceCampaignGateway()
    try:
        _sobject, campaign_id = crm.parse_record_link(
            str(args.get("campaign_link", "")), {"Campaign"}
        )
        campaign = gateway.get_record("Campaign", campaign_id)
        links: dict[int, str] = {}
        for item in args.get("member_links", []) or []:
            if isinstance(item, dict):
                links[int(item.get("grant_lead_id", 0))] = str(
                    item.get("salesforce_link", "")
                )
        conn = db.connect()
        lead_ids = [int(item) for item in args.get("lead_ids", [])]
        snapshot_id = str(args.get("search_request_id", ""))
        if snapshot_id:
            snapshot = db.get_search_request(conn, snapshot_id, requester_slack)
            expected_session = f"{workspace}:{channel}:{thread_ts}:{requester_slack}"
            if snapshot is None or snapshot["session_key"] != expected_session:
                raise PermissionError(
                    "search snapshot is stale or belongs to another thread"
                )
            stored_ids = json.loads(str(snapshot["result_lead_ids_json"]))
            if (
                not bool(snapshot["result_complete"])
                or snapshot["total_count"] is None
                or len(stored_ids) != int(snapshot["total_count"])
            ):
                raise ValueError(
                    "search snapshot is incomplete; run the complete state/tier batch tool"
                )
            lead_ids = [int(item) for item in stored_ids]
        action = crm.prepare_membership(
            conn,
            gateway,
            workspace,
            channel,
            thread_ts,
            requester_slack,
            campaign,
            lead_ids,
            supplied_links=links,
            allow_org_leads=bool(args.get("allow_org_leads", False)),
            allow_resolved_only=bool(args.get("allow_resolved_only", False)),
        )
    except (ValueError, PermissionError, KeyError, requests.RequestException) as exc:
        return f"ERROR: Campaign member preview failed ({type(exc).__name__}): {str(exc)[:180]}"
    return _crm_action_result(
        action.action_id, action.nonce, action.preview, action.expires_at
    )


def find_person_linkedin(
    entity: str,
    state: str,
    on_progress: Progress | None = None,
    lead_id: int = 0,
    person_name: str = "",
) -> str:
    """LinkedIn profile of a decision-maker (name/title/link, no email).

    When lead_id names a real Grant lead, the person is persisted as a
    linkedin_only contact so a Salesforce record can later be built from it.

    person_name means the rep asked about a SPECIFIC human. The answer is then that
    person or nobody: returning the next plausible profile would attribute a real
    stranger to the name the rep typed, and persist them toward a CRM record."""
    from ..enrich import finder

    person = finder.linkedin_person(
        entity, state, on_progress=on_progress, person_name=person_name
    )
    if person is None:
        if person_name.strip():
            return (
                f"I couldn't confirm a LinkedIn profile for {person_name.strip()} at "
                f"{entity}. I won't guess at someone else — searching by role instead "
                "would give you a different person under their name."
            )
        return "No clear LinkedIn profile found for their decision-maker."
    role = f", {person['title']}" if person["title"] else ""
    saved = ""
    if lead_id > 0:
        conn = db.connect()
        lead = conn.execute(
            "SELECT id FROM leads WHERE id=?", (int(lead_id),)
        ).fetchone()
        if lead is not None:
            title = str(person["title"] or "")
            # LinkedIn snippets often put the ORG into the title slot; a title
            # that is just the organization name is no title at all.
            if (
                db.canonical_entity_key(title).partition("|")[0]
                == db.canonical_entity_key(entity).partition("|")[0]
            ):
                title = ""
            contact_id = db.save_linkedin_contact(
                conn,
                int(lead_id),
                str(person["name"]),
                title,
                str(person["url"]),
            )
            saved = (
                f" Saved as contact #{contact_id} on lead {lead_id} "
                "(LinkedIn-only: profile ownership not verified, no email)."
            )
    return (
        f"LinkedIn: {person['name']}{role} — {person['url']} "
        f"(reach out via LinkedIn; no email verified).{saved}"
    )


def salesforce_contact_record_preview(
    args: dict[str, Any],
    requester_slack: str,
    workspace: str,
    channel: str,
    thread_ts: str,
) -> str:
    """Persist a requester-bound contact-record preview and return its marker."""
    from ..enrich import salesforce_contact_records as records
    from ..enrich.salesforce_campaign_gateway import SalesforceCampaignGateway

    conn = db.connect()
    lead_id = int(args.get("lead_id", 0) or 0)
    if lead_id <= 0 and args.get("entity"):
        # A natural "add <person> to Salesforce" after finding the contact by org
        # name carries no lead number. Resolve it here (same resolver find_contact
        # uses) so the flow doesn't dead-end asking the rep for an id it never saw.
        resolved = resolve_lead_by_name(
            conn, str(args["entity"]), str(args.get("state") or "")
        )
        if isinstance(resolved, str):  # already an honest "ERROR: which lead?" message
            return resolved
        lead_id = resolved
    try:
        action = records.prepare_contact_record(
            conn,
            SalesforceCampaignGateway(),
            workspace,
            channel,
            thread_ts,
            requester_slack,
            lead_id,
            int(args["contact_id"]) if args.get("contact_id") is not None else None,
        )
    except (ValueError, PermissionError, KeyError, requests.RequestException) as exc:
        return (
            "ERROR: contact record preview failed "
            f"({type(exc).__name__}): {str(exc)[:200]}"
        )
    return _crm_action_result(
        action.action_id, action.nonce, action.preview, action.expires_at
    )


def _zoominfo_fill_many(
    lead_ids: list[int], max_credits: int, confirm: bool, requester_slack: str
) -> str:
    """Price, or buy, decision-maker contacts across several leads at once.

    THE GAP THIS CLOSES. A rep asked "Do it for all" and there was no way to say yes:
    every contact had to be bought one lead at a time through its own approval
    conversation, so 997 of 1000 purchased credits sat unused beside 62 contacts with
    no email, phone or mobile at all. The engine already existed as a CLI command;
    reps do not have a terminal.

    `confirm=false` runs only FREE searches and reports the exact bill, which is what
    makes the approval real rather than a formality — the rep sees the number before
    anyone spends it.
    """
    from .. import contact_fill, db

    if not lead_ids:
        return "ERROR: tell me which leads to fill."
    if max_credits <= 0:
        return "ERROR: I need a credit ceiling above zero before I can price this."

    conn = db.connect()
    remaining = contact_fill.remaining_credits(conn)
    if max_credits > remaining:
        return (
            f"ERROR: that ceiling is {max_credits} credits but only {remaining} "
            "remain this period. Lower it and I'll price the run."
        )
    outcome = contact_fill.fill_contacts(
        conn,
        lead_ids,
        max_credits=max_credits,
        dry_run=not confirm,
        requested_by=requester_slack,
    )
    if not confirm:
        return (
            f"PRICED, NOTHING SPENT: {outcome.summary()}. "
            f"{remaining} credits remain. "
            f"{model_note('Show the rep this exact cost and ask for a yes before calling again with confirm=true.')}"
        )
    return (
        f"BOUGHT: {outcome.summary()}. "
        f"{contact_fill.remaining_credits(conn)} credits remain."
    )


def _log_tool_failure(tool: str) -> None:
    """Print the active exception to stderr so bot.log preserves the traceback.

    The model only ever sees the exception's class name; without this the real
    error text exists nowhere once the tool call returns."""
    print(f"[tool-error] {tool}: unhandled exception follows", file=sys.stderr)
    traceback.print_exc()


def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    on_progress: Progress | None = None,
    requester_slack: str = "",
    workspace: str = "",
    channel: str = "",
    thread_ts: str = "",
) -> tuple[str, GeneratedArtifact | None]:
    """Dispatch one tool call and return text plus an optional owned artifact.

    on_progress emits short status phrases for Grant's live spinner; requester_slack
    is the rep asking (needed for a Google Sheet in their own Google account)."""
    p = on_progress or _NOOP
    if name == "source_inventory_status":
        p("Checking source discovery evidence")
        return source_inventory_status(
            view=str(args.get("view", "summary")),
            state=str(args.get("state", "")),
            namespace=str(args.get("namespace", "all")),
            limit=int(args.get("limit", 10) or 10),
        ), None
    if name == "web_search":
        return web_search(str(args.get("query", "")), p), None
    if name == "salesforce_lookup":
        try:
            return salesforce_lookup(
                str(args.get("entity", "")),
                str(args.get("domain", "")),
                str(args.get("phone", "")),
                str(args.get("state", "")),
                p,
            ), None
        except Exception as exc:
            _log_tool_failure("salesforce_lookup")
            return f"ERROR: Salesforce lookup failed ({type(exc).__name__}).", None
    if name == "salesforce_campaign_status":
        p("Checking that Campaign")
        try:
            return salesforce_campaign_status(str(args.get("name_or_link", ""))), None
        except Exception as exc:
            _log_tool_failure("salesforce_campaign_status")
            return f"ERROR: Campaign status failed ({type(exc).__name__}).", None
    if name == "salesforce_campaign_search":
        p("Searching Salesforce Campaigns")
        return salesforce_campaign_search(str(args.get("name_or_link", ""))), None
    if name == "salesforce_campaign_create_preview":
        p("Preparing Campaign preview")
        return salesforce_campaign_create_preview(
            args, requester_slack, workspace, channel, thread_ts
        ), None
    if name == "salesforce_campaign_members_preview":
        p("Resolving Campaign members")
        return salesforce_campaign_members_preview(
            args, requester_slack, workspace, channel, thread_ts
        ), None
    if name == "salesforce_campaign_batch_preview":
        p("Freezing complete Campaign batches")
        return salesforce_campaign_batch_preview(
            args, requester_slack, workspace, channel, thread_ts
        ), None
    if name == "lead_stats":
        p("Checking the lead database")
        return lead_stats(
            group_by=str(args.get("group_by", "grade")),
            state=str(args.get("state", "")),
            program=str(args.get("program", "")),
            grade=str(args.get("grade", "")),
        ), None
    if name == "search_leads":
        try:
            return search_leads(
                state=str(args.get("state", "")),
                org_type=str(args.get("org_type", "")),
                program=str(args.get("program", "")),
                grade=str(args.get("grade", "")),
                record_kind=str(args.get("record_kind", "")),
                amount_min=(
                    float(args["amount_min"])
                    if args.get("amount_min") is not None
                    else None
                ),
                amount_max=(
                    float(args["amount_max"])
                    if args.get("amount_max") is not None
                    else None
                ),
                enrollment_min=(
                    int(args["enrollment_min"])
                    if args.get("enrollment_min") is not None
                    else None
                ),
                enrollment_max=(
                    int(args["enrollment_max"])
                    if args.get("enrollment_max") is not None
                    else None
                ),
                city=str(args.get("city", "")),
                name_contains=str(args.get("name_contains", "")),
                date_field=str(args.get("date_field", "")),
                date_from=str(args.get("date_from", "")),
                date_to=str(args.get("date_to", "")),
                open_only=bool(args.get("open_only", False)),
                limit=int(args.get("limit", 50) or 50),
                export=args.get("export", ""),
                result_scope=str(args.get("result_scope", "top_n")),
                with_contacts=bool(args.get("with_contacts", False)),
                on_progress=p,
                requester_slack=requester_slack,
                workspace=workspace,
                channel=channel,
                thread_ts=thread_ts,
            )
        except Exception as exc:
            _log_tool_failure("search_leads")
            return f"ERROR: search failed ({type(exc).__name__}).", None
    if name == "find_contact":
        try:
            return find_contact(
                int(args.get("lead_id", 0) or 0),
                p,
                entity=str(args.get("entity", "")),
                state=str(args.get("state", "")),
            ), None
        except Exception as exc:  # enrichment API hiccup -> honest tool error
            _log_tool_failure("find_contact")
            return f"ERROR: enrichment failed ({type(exc).__name__}) — say so.", None
    if name == "find_person_linkedin":
        try:
            return find_person_linkedin(
                str(args.get("entity", "")),
                str(args.get("state", "")),
                p,
                lead_id=int(args.get("lead_id", 0) or 0),
                person_name=str(args.get("person_name", "")),
            ), None
        except Exception as exc:
            _log_tool_failure("find_person_linkedin")
            return f"ERROR: LinkedIn search failed ({type(exc).__name__}).", None
    if name == "zoominfo_contact_preview":
        p("Checking ZoomInfo (free search)")
        try:
            return zoominfo_contact_preview(
                int(args.get("lead_id", 0) or 0), str(args.get("job_title", ""))
            ), None
        except Exception as exc:
            _log_tool_failure("zoominfo_contact_preview")
            return f"ERROR: ZoomInfo preview failed ({type(exc).__name__}).", None
    if name == "zoominfo_enrich_contacts":
        p("Pulling ZoomInfo contacts")
        try:
            return zoominfo_enrich_contacts(
                int(args.get("lead_id", 0) or 0),
                [str(item) for item in (args.get("person_ids") or [])],
                requester_slack,
            ), None
        except Exception as exc:
            _log_tool_failure("zoominfo_enrich_contacts")
            return f"ERROR: ZoomInfo pull failed ({type(exc).__name__}).", None
    if name == "zoominfo_fill_many":
        p("Pricing a bulk contact pull")
        try:
            return _zoominfo_fill_many(
                [int(i) for i in (args.get("lead_ids") or [])],
                int(args.get("max_credits", 0) or 0),
                bool(args.get("confirm")),
                requester_slack,
            ), None
        except Exception as exc:  # noqa: BLE001 — a tool reports, it never kills a turn
            _log_tool_failure("zoominfo_fill_many")
            return f"ERROR: bulk contact fill failed ({type(exc).__name__}).", None
    if name in {
        "reminder_set",
        "reminder_list",
        "reminder_cancel",
        "stop_followups",
        "email_results",
    }:
        from . import reminder_tools

        # Every other tool group here has an error boundary; this one did not, so a
        # model writing reminder_id: "the texas one" turned an int() ValueError into
        # the whole turn dying as "I'm having trouble thinking right now" — the exact
        # reply that ended four real conversations.
        try:
            return _run_reminder_tool(
                reminder_tools, name, args, requester_slack, channel, thread_ts
            ), None
        except Exception as exc:  # noqa: BLE001 — a tool reports, it never kills a turn
            _log_tool_failure(name)
            return f"ERROR: {name} failed ({type(exc).__name__}).", None
    if name == "fetch_url":
        return fetch_url(str(args.get("url", "")), p), None
    if name == "record_contact_fact":
        p("Recording what you told me")
        try:
            return record_contact_fact(
                int(args.get("lead_id", 0) or 0),
                requester_slack,
                name=str(args.get("name", "")),
                title=str(args.get("title", "")),
                email=str(args.get("email", "")),
                phone=str(args.get("phone", "")),
            ), None
        except Exception as exc:
            _log_tool_failure("record_contact_fact")
            return f"ERROR: couldn't record that ({type(exc).__name__}).", None
    if name == "salesforce_contact_record_preview":
        p("Preparing Salesforce contact record preview")
        return salesforce_contact_record_preview(
            args, requester_slack, workspace, channel, thread_ts
        ), None
    return f"ERROR: unknown tool {name}", None


def _run_reminder_tool(
    reminder_tools: Any,
    name: str,
    args: dict[str, Any],
    requester_slack: str,
    channel: str,
    thread_ts: str,
) -> str:
    """Dispatch one reminder/opt-out/email tool to its implementation."""
    if name == "reminder_set":
        return str(
            reminder_tools.reminder_set(args, requester_slack, channel, thread_ts)
        )
    if name == "reminder_list":
        return str(reminder_tools.reminder_list(requester_slack))
    if name == "reminder_cancel":
        return str(reminder_tools.reminder_cancel(args, requester_slack))
    if name == "stop_followups":
        return str(
            reminder_tools.stop_followups(args, requester_slack, channel, thread_ts)
        )
    return str(reminder_tools.email_results(args, requester_slack, channel, thread_ts))


def run_tool(
    name: str,
    args: dict[str, Any],
    on_progress: Progress | None = None,
    requester_slack: str = "",
    workspace: str = "",
    channel: str = "",
    thread_ts: str = "",
) -> tuple[str, GeneratedArtifact | None]:
    """Dispatch one tool call, then sanitize its text before anything else sees it.

    This is a trust boundary, not a formatting step. conversation.py harvests
    <grant-crm-action> markers out of TOOL RESULTS and grant.py renders them as real
    Salesforce approval buttons, so a tool that returns text from the open web could
    otherwise mint a button in Grant's voice. Only _ACTION_PRODUCING_TOOLS may carry a
    marker out of here; every other tool's output is stripped, whatever it contains.
    """
    text, artifact = _dispatch_tool(
        name,
        args,
        on_progress,
        requester_slack=requester_slack,
        workspace=workspace,
        channel=channel,
        thread_ts=thread_ts,
    )
    if name not in _ACTION_PRODUCING_TOOLS:
        text = strip_action_markers(text)
    # The model SHOULD read the guidance a tool attaches; it just should not see the
    # delimiters. Human-facing surfaces that post a tool result unmediated call
    # presentation.for_human instead, which removes the guidance itself.
    return for_model(text), artifact
