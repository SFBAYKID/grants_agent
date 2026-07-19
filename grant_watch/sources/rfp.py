"""Security-RFP discovery source — OPEN school/city physical-security solicitations.

VERIFICATION: needs-testing (live smoke gated behind RFP_DISCOVERY_ENABLED). Feasibility
proven live 2026-07-18: Firecrawl search surfaced real current listings (City of Kemah
TX RFP 2026-05, City of Woodland WA police cameras, Irvington NJ) and their official
`.gov`/`.us` pages scrape with verbatim entity/due-date/status.

This is a paid, LLM-backed DISCOVERY probe, not an exhaustive feed — the query set below
IS the coverage, and it samples what Firecrawl ranks that run. It is therefore:
  * wired into cli._active_pollers() ONLY behind RFP_DISCOVERY_ENABLED (like SAM.gov),
    never the free static POLLERS list;
  * hard-capped on total Firecrawl calls per run;
  * per-query fault-isolated — one failing query never fails the rest, and if EVERY
    query fails we raise SourceUnreachable (Constitution rule 1: "could not look" is
    never recorded as "no open RFPs").

The model output is untrusted; ALL trust-bearing logic lives in the pure `rfp_parse`
module (see its header). This file is only live I/O + the extraction prompt.
"""

from __future__ import annotations

import json
import sys
from datetime import date

from anthropic import Anthropic

from ..models import RawItem
from ..enrich.finder import MODEL, SourceUnreachable, _scrape, _search
from . import rfp_parse

# The coverage IS these queries (architectural-critic H3) — a labeled sampling probe,
# reviewed in code review. School + city physical-security solicitation angles.
_SEARCH_QUERIES: tuple[str, ...] = (
    "school district security camera system RFP request for proposals responses due",
    "school district door access control system RFP proposals due",
    "city police department video surveillance camera RFP proposals due",
    "city hall access control door security RFP request for proposals due",
    "county security camera surveillance system request for proposals due",
    "school security vestibule entry control RFP bid proposals due",
)
_RESULTS_PER_QUERY = 4  # top-ranked official pages to consider per query
_MAX_FIRECRAWL_CALLS = 40  # hard ceiling on search+scrape per run (cost backstop)


def _extract_rfp(page_text: str, url: str) -> dict[str, str]:
    """Claude reads ONE scraped page and returns the RFP fields, or an empty dict.

    Untrusted: every field is re-checked verbatim/adjacency in rfp_parse. The prompt
    only steers the model toward the right fields — it is never the source of truth.
    Critically it must copy the SUBMISSION-deadline date EXACTLY as printed (so the
    page-adjacency gate can find it) and identify the GOVERNMENT issuing the RFP.
    """
    client = Anthropic()
    prompt = (
        "Below is one page scraped from a government procurement website. If it is a "
        "SINGLE open Request for Proposals/Qualifications/bid for PHYSICAL security "
        "(security cameras, video surveillance, access control, door hardening, "
        "alarms), extract its fields. Use ONLY text on this page.\n\n"
        "Rules:\n"
        "- entity: the GOVERNMENT issuing the RFP (e.g. 'City of Kemah', "
        "'Irvington School District') — never a vendor, architect, or contractor.\n"
        "- due_date: the deadline for SUBMITTING proposals, copied EXACTLY as printed "
        "on the page (e.g. 'May 28, 2026' or 'Fri, 01/30/2026 - 2:00 PM'). NOT a "
        "pre-bid meeting, questions-due, addendum, or award date. If no submission "
        "deadline is printed, use \"\".\n"
        "- rfp_number, title, state (2-letter), status, portal: copy verbatim or \"\".\n"
        "- If this is a LIST of multiple solicitations, or not physical security, "
        "return null.\n\n"
        'Respond with ONLY JSON: {"entity":"...","state":"...","rfp_number":"...",'
        '"title":"...","due_date":"...","status":"...","portal":"..."} or null.\n\n'
        f"PAGE ({url}):\n{page_text[:24000]}"
    )
    message = client.messages.create(
        model=MODEL, max_tokens=400, messages=[{"role": "user", "content": prompt}]
    )
    raw = "".join(b.text for b in message.content if b.type == "text").strip()
    if raw.lower().startswith("null"):
        return {}
    try:
        data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}
    return {k: str(v or "") for k, v in data.items()} if isinstance(data, dict) else {}


def poll(today: date | None = None) -> list[RawItem]:
    """Discover OPEN physical-security RFPs. Returns verified RawItems (may be []).

    Return/raise contract (mirrors finder.py): a list of verified items, or raise
    SourceUnreachable when NOT ONE query reached Firecrawl (so the caller records
    'could not look', never a false 'no RFPs this week'). Partial failure (some queries
    threw) returns what succeeded and logs the shortfall — a probe is partial by design.
    """
    today = today or date.today()
    out: list[RawItem] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    reached_search = False
    failed_queries = 0
    calls = 0

    for query in _SEARCH_QUERIES:
        if calls >= _MAX_FIRECRAWL_CALLS:
            break
        try:
            results = _search(query, limit=_RESULTS_PER_QUERY + 2)
            calls += 1
        except Exception as exc:  # noqa: BLE001 — one query's outage is not the run's
            failed_queries += 1
            print(f"[rfp] search failed for {query!r}: {exc}", file=sys.stderr)
            continue
        reached_search = True
        considered = 0
        for result in results:
            if calls >= _MAX_FIRECRAWL_CALLS or considered >= _RESULTS_PER_QUERY:
                break
            url = str(result.get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            considered += 1
            page_text = _scrape(url)
            calls += 1
            if len(page_text) < 200 or rfp_parse.is_index_page(page_text):
                continue  # blocked/thin page, or a multi-RFP index (never fabricate)
            try:
                extracted = _extract_rfp(page_text, url)
            except Exception:  # noqa: BLE001 — one page's API hiccup is inconclusive
                continue
            if not extracted:
                continue
            item = rfp_parse.build_rawitem(extracted, page_text, url, today)
            if item is not None and item.item_id not in seen_ids:
                seen_ids.add(item.item_id)
                out.append(item)

    if not reached_search:
        raise SourceUnreachable("RFP discovery could not reach any search")
    if failed_queries:
        print(
            f"[rfp] PARTIAL: {failed_queries}/{len(_SEARCH_QUERIES)} queries failed — "
            "coverage is a sample, not exhaustive.",
            file=sys.stderr,
        )
    return out
