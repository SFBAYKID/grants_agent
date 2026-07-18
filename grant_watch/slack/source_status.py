"""Read-only Slack presentation of validated source-discovery evidence.

Why: Grant users work in Slack, but raw Firecrawl batches and Census coverage CSVs
are operator artifacts rather than leads. This module turns only safe aggregate and
reviewed catalog fields into concise Slack text. It performs no HTTP requests, reads
no credentials, exposes no raw search payloads, and cannot start paid discovery.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ..coverage_universe import TASK_ROOT as COUNTY_TASK_ROOT
from ..coverage_universe import load_county_tasks
from ..entity_coverage import EntityCoverageTask, load_entity_tasks
from ..incorporated_place_universe import TASK_ROOT as PLACE_TASK_ROOT
from ..school_district_universe import TASK_ROOT as SCHOOL_TASK_ROOT
from ..source_catalog import CATALOG_PATH, SourceCatalogEntry, load_catalog
from ..source_discovery import CHECKS_PATH, DiscoveryCheck, load_discovery_checks
from ..source_discovery_batch import BATCH_ROOT, validate_stored_batches
from ..source_discovery_models import BatchManifest
from ..source_discovery_store import load_checkpoints, load_manifest


ALLOWED_VIEWS = frozenset({"summary", "coverage", "reviewed_sources", "recent_batches"})
ALLOWED_NAMESPACES = frozenset(
    {"all", "county", "school_district", "incorporated_place"}
)
NAMESPACE_LABELS = {
    "county": "counties",
    "school_district": "school districts",
    "incorporated_place": "incorporated places",
}
LEVEL_FOR_NAMESPACE = {
    "county": "county",
    "school_district": "school_district",
    "incorporated_place": "city",
}
STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
SOURCE_STATUS_TOOL_SCHEMA: dict[str, object] = {
    "name": "source_inventory_status",
    "description": (
        "Read-only status for Grant's internal source-discovery inventory. Use for "
        "catalog counts, Census research coverage, reviewed source candidates, and "
        "validated raw batch summaries. It never runs Firecrawl or creates leads."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "view": {
                "type": "string",
                "enum": [
                    "summary",
                    "coverage",
                    "reviewed_sources",
                    "recent_batches",
                ],
            },
            "state": {"type": "string", "description": "two-letter state or DC"},
            "namespace": {
                "type": "string",
                "enum": [
                    "all",
                    "county",
                    "school_district",
                    "incorporated_place",
                ],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        },
        "required": [],
    },
}


@dataclass(frozen=True)
class DiscoveryStatusPaths:
    """Injectable paths for validated, repository-owned discovery evidence."""

    catalog: Path = CATALOG_PATH
    checks: Path = CHECKS_PATH
    county_tasks: Path = COUNTY_TASK_ROOT
    school_tasks: Path = SCHOOL_TASK_ROOT
    place_tasks: Path = PLACE_TASK_ROOT
    batches: Path = BATCH_ROOT


@dataclass(frozen=True)
class SlackStatusRequest:
    """One deterministic read-only source-inventory request parsed from Slack."""

    view: str
    state: str
    namespace: str
    limit: int
    paid_execution_requested: bool


def _safe_text(value: object, limit: int = 180) -> str:
    """Flatten and Slack-escape one reviewed field before rendering it."""
    text = " ".join(str(value).split())[:limit]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_url(value: str) -> str:
    """Return one bounded public HTTPS URL without credentials or Slack markup."""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return "(URL unavailable)"
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query)}
    has_sensitive_query = any(
        re.search(
            r"(?:^|[_-])(api[_-]?key|access[_-]?key|auth|credential|password|secret|signature|token)(?:$|[_-])",
            key,
        )
        for key in query_keys
    )
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or has_sensitive_query
        or any(character in value for character in "<>\r\n")
    ):
        return "(URL unavailable)"
    return _safe_text(value, 500)


def _state_from_text(text: str) -> str:
    """Extract a US state name or explicit two-letter code from natural language."""
    lowered = text.lower()
    for name in sorted(STATE_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return STATE_NAMES[name]
    match = re.search(r"(?:\bin\s+|\bfor\s+|\bfrom\s+|\bstate\s+)([A-Za-z]{2})\b", text)
    if match is None:
        # An all-caps code is explicit even when placed between words, as in
        # "reviewed NH sources". Lowercase two-letter words remain ignored.
        match = re.search(r"\b([A-Z]{2})\b", text)
    if match is not None:
        code = match.group(1).upper()
        if code in STATE_NAMES.values():
            return code
    return ""


def _namespace_from_text(text: str) -> str:
    """Map common Slack wording to one canonical Census research namespace."""
    lowered = text.lower()
    if re.search(r"\b(county|counties)\b", lowered):
        return "county"
    if re.search(r"\b(school district|school districts|districts)\b", lowered):
        return "school_district"
    if re.search(r"\b(city|cities|town|towns|municipal|places)\b", lowered):
        return "incorporated_place"
    return "all"


def parse_status_request(
    user_text: str, thread_context: list[str] | None = None
) -> SlackStatusRequest | None:
    """Recognize source-inventory asks and parse a bounded deterministic request."""
    context = " ".join(thread_context[-4:]) if thread_context else ""
    combined = f"{context} {user_text}".strip()
    lowered = combined.lower()
    current = user_text.lower()
    paid_action = re.match(
        r"^\s*(?:please\s+|grant[, :]*)?"
        r"(?:go\s+)?"
        r"(?:can you\s+|could you\s+|would you\s+|i want you to\s+)?"
        r"(?:run|start|launch|execute|search(?:\s+for)?|find)\b",
        current,
    )
    paid_execution = bool(
        paid_action
        and re.search(
            r"\b(discovery|firecrawl|source research|new sources?)\b", current
        )
    )
    inventory_context = (
        paid_execution
        or any(
            phrase in lowered
            for phrase in (
                "source discovery",
                "discovery status",
                "source inventory",
                "research coverage",
                "sources reviewed",
                "reviewed sources",
                "sources remaining",
                "sources have we found",
                "firecrawl batch",
                "discovery batch",
                "recent discoveries",
                "raw discovery search",
            )
        )
        or bool(
            re.search(r"\bschool district research\b", lowered)
            or re.search(r"\breviewed\b.*\bsources?\b", lowered)
            or re.search(r"\bgrant\s+(?:actually\s+)?reviewed\b", lowered)
            or re.search(r"\bnot (?:yet )?researched\b", lowered)
        )
    )
    if not inventory_context:
        return None
    if "batch" in current or "raw discovery search" in current:
        view = "recent_batches"
    elif "recent discoveries" in current:
        view = "reviewed_sources"
    elif any(word in current for word in ("reviewed", "promoted", "list sources")):
        view = "reviewed_sources"
    elif (
        "school district research" in current
        or "not researched" in current
        or any(
            word in current
            for word in (
                "coverage",
                "remaining",
                "left",
                "researched",
                "counties",
                "districts",
                "cities",
            )
        )
    ):
        view = "coverage"
    else:
        view = "summary"
    count_match = re.search(r"\b(?:top|last|show)\s+(\d{1,2})\b", current)
    limit = min(25, max(1, int(count_match.group(1)))) if count_match else 10
    return SlackStatusRequest(
        view=view,
        state=_state_from_text(user_text),
        namespace=_namespace_from_text(user_text),
        limit=limit,
        paid_execution_requested=paid_execution,
    )


def _filtered_tasks(
    tasks: list[EntityCoverageTask], state: str
) -> list[EntityCoverageTask]:
    """Apply an optional exact state filter to one typed task universe."""
    return [task for task in tasks if not state or task.state == state]


def _coverage_counts(
    paths: DiscoveryStatusPaths, state: str, namespace: str
) -> list[tuple[str, Counter[str]]]:
    """Load exact research-state counts without inferring integration coverage."""
    rows: list[tuple[str, Counter[str]]] = []
    if namespace in {"all", "county"}:
        county = [
            task
            for task in load_county_tasks(paths.county_tasks)
            if not state or task.state == state
        ]
        rows.append(("county", Counter(task.research_status for task in county)))
    if namespace in {"all", "school_district"}:
        school = _filtered_tasks(load_entity_tasks(paths.school_tasks), state)
        rows.append(
            ("school_district", Counter(task.research_status for task in school))
        )
    if namespace in {"all", "incorporated_place"}:
        places = _filtered_tasks(load_entity_tasks(paths.place_tasks), state)
        rows.append(
            ("incorporated_place", Counter(task.research_status for task in places))
        )
    return rows


def _scope(entries: list[SourceCatalogEntry], state: str) -> list[SourceCatalogEntry]:
    """Filter catalog rows to an exact state when the user supplied one."""
    return [entry for entry in entries if not state or entry.state == state]


# Plain-English phrasings for the machine vocabularies, so Slack answers read like
# a human status update instead of enum tokens. Unknown values fall back to the raw
# (safe-escaped) string rather than being hidden.
_ACCESS_MODE_WORDS = {
    "public_no_auth": "open, no login",
    "public_api_key": "open with a free API key",
    "free_account": "free account needed",
    "supplier_account": "vendor account needed",
    "paid": "paid access",
    "manual_only": "manual download only",
    "unknown": "access not yet checked",
}
_ACCESS_STATUS_WORDS = {
    "verified": "confirmed",
    "assumed": "assumed",
    "needs-testing": "not yet tested",
}
_INTEGRATION_WORDS = {
    "discovered": "found, not yet checked",
    "access_checked": "access confirmed, no feed built yet",
    "parser_tested": "parser tested",
    "live_zero_verified": "live, no security matches yet",
    "live_positive_verified": "live and finding matches",
}
# Order matters: the most useful categories are named first in a coverage line.
_COVERAGE_WORDS = (
    ("candidate_found", "with a source link"),
    ("not_applicable", "no separate source needed"),
    ("researched_not_found", "researched, nothing found"),
    ("not_researched", "not yet researched"),
)
_BATCH_STATUS_WORDS = {
    "success": "completed",
    "zero_results": "came back empty",
    "non_retryable_failure": "failed",
    "retryable_failure": "failed but will retry",
    "in_flight": "still running",
}
# Sentence fragments for reviewed-source integration stages: each reads naturally
# after "…, and". Unknown values fall back to the raw safe-escaped token.
_STAGE_SENTENCES = {
    "discovered": "we've found it but haven't checked it yet",
    "access_checked": "we've confirmed access but haven't built a feed yet",
    "parser_tested": "its parser has been tested",
    "live_zero_verified": "it's live but hasn't found security matches yet",
    "live_positive_verified": "it's live and finding matches",
}
_COUNT_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)


def _count_phrase(count: int) -> str:
    """Spell small counts in words so sentences read like a colleague wrote them."""
    return _COUNT_WORDS[count] if 0 <= count <= 10 else f"{count:,}"


def _plural(count: int, singular: str, plural: str) -> str:
    """Pick the grammatical form matching one exact count."""
    return singular if count == 1 else plural


def _article(noun_phrase: str) -> str:
    """Choose a/an for the following noun phrase without overthinking edge cases."""
    return "an" if noun_phrase[:1].lower() in "aeiou" else "a"


def _human_date(iso_date: str) -> str:
    """Turn '2026-07-16' into 'July 16, 2026' without inventing a date on bad input."""
    try:
        parsed = date.fromisoformat(iso_date)
    except ValueError:
        return _safe_text(iso_date)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _coverage_line(label: str, counts: Counter[str]) -> str:
    """One human coverage line: 'Counties: 3,144 in total — 56 with a source link…'."""
    total = sum(counts.values())
    parts = [
        f"{counts[key]:,} {phrase}" for key, phrase in _COVERAGE_WORDS if counts.get(key)
    ]
    detail = " — " + ", ".join(parts) if parts else ""
    return f"- {label[:1].upper()}{label[1:]}: {total:,} in total{detail}"


def _render_coverage(paths: DiscoveryStatusPaths, state: str, namespace: str) -> str:
    """Render coverage queues in plain English, keeping every research-state count."""
    scope = f" for {state}" if state else " nationwide"
    lines = [f"Source research coverage{scope}:"]
    for key, counts in _coverage_counts(paths, state, namespace):
        lines.append(_coverage_line(NAMESPACE_LABELS[key], counts))
    lines.append(
        'A "source link" means we found a page worth reviewing — not a working feed '
        "or a lead yet."
    )
    return "\n".join(lines)


def _render_reviewed_sources(
    paths: DiscoveryStatusPaths, state: str, namespace: str, limit: int
) -> str:
    """Render only catalog sources backed by immutable selected-result checks."""
    catalog = {entry.source_id: entry for entry in load_catalog(paths.catalog)}
    level = LEVEL_FOR_NAMESPACE.get(namespace, "")
    checks = sorted(
        load_discovery_checks(paths.checks),
        key=lambda check: (check.checked_on, check.check_id),
        reverse=True,
    )
    reviewed: list[tuple[DiscoveryCheck, SourceCatalogEntry]] = []
    seen: set[str] = set()
    for check in checks:
        entry = catalog.get(check.research_key)
        if entry is None or entry.source_id in seen:
            continue
        if state and entry.state != state:
            continue
        if level and entry.jurisdiction_level.value != level:
            continue
        seen.add(entry.source_id)
        reviewed.append((check, entry))
    scope = f" in {state}" if state else ""
    if not reviewed:
        return (
            f"I haven't reviewed any sources{scope} matching those filters yet — "
            "want to see the research coverage instead?"
        )
    shown = reviewed[:limit]
    if len(reviewed) > len(shown):
        opener = (
            f"Here are the {_count_phrase(len(shown))} most recently reviewed "
            f"sources{scope} — we've reviewed {_count_phrase(len(reviewed))} in all:"
        )
    else:
        newest = ", newest first" if len(shown) > 1 else ""
        opener = (
            f"Here {_plural(len(shown), 'is', 'are')} the {_count_phrase(len(shown))} "
            f"{_plural(len(shown), 'source', 'sources')} we've reviewed{scope}{newest}:"
        )
    lines = [opener]
    for index, (check, entry) in enumerate(shown, start=1):
        level = _safe_text(entry.jurisdiction_level.value.replace("_", " "))
        mode = _ACCESS_MODE_WORDS.get(
            entry.access_mode.value, _safe_text(entry.access_mode.value)
        )
        access_status = _ACCESS_STATUS_WORDS.get(
            entry.access_status.value, _safe_text(entry.access_status.value)
        )
        stage = _STAGE_SENTENCES.get(
            entry.integration_status.value,
            _INTEGRATION_WORDS.get(
                entry.integration_status.value,
                _safe_text(entry.integration_status.value),
            ),
        )
        lines.append(
            f"{index}. {_safe_text(entry.name)} — {_article(level)} {level} source "
            f"we reviewed on {_human_date(check.checked_on)}. "
            f"Access is {mode} ({access_status}), and {stage}. "
            f"{_safe_url(entry.url)}"
        )
    lines.append(
        "These are pages we've reviewed as candidates — not live feeds or leads "
        "unless we say a source is live."
    )
    return "\n".join(lines)


def _batch_date(batch_id: str) -> str:
    """Turn a '20260716T004633Z' batch id into a readable '2026-07-16' date."""
    match = re.match(r"(\d{4})(\d{2})(\d{2})", batch_id)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return _safe_text(batch_id)


def _batch_status_phrase(statuses: Counter[str]) -> str:
    """Describe search outcomes in words, keeping every non-success state visible."""
    named = (
        "success",
        "zero_results",
        "retryable_failure",
        "non_retryable_failure",
        "in_flight",
    )
    parts = [
        f"{_count_phrase(statuses[key])} {_BATCH_STATUS_WORDS[key]}"
        for key in named
        if statuses.get(key)
    ]
    parts.extend(
        f"{_count_phrase(count)} {_safe_text(status)}"
        for status, count in sorted(statuses.items())
        if status not in named
    )
    return ", ".join(parts)


def _batch_search_counts(task_count: int, attempt_count: int) -> str:
    """Say how many searches ran, mentioning retries only when they happened."""
    phrase = f"{_count_phrase(task_count)} {_plural(task_count, 'search', 'searches')}"
    if attempt_count != task_count:
        phrase += (
            f" over {_count_phrase(attempt_count)} "
            f"{_plural(attempt_count, 'attempt', 'attempts')}"
        )
    return phrase


def _batch_result_counts(result_count: int) -> str:
    """Report the exact result count in plain words, honest about zero."""
    if result_count == 0:
        return "found no results"
    return (
        f"found {_count_phrase(result_count)} potential "
        f"{_plural(result_count, 'result', 'results')}"
    )


def _render_recent_batches(
    paths: DiscoveryStatusPaths, state: str, namespace: str, limit: int
) -> str:
    """Render validated batch aggregates without exposing raw Firecrawl payloads."""
    summaries = {
        summary.batch_id: summary for summary in validate_stored_batches(paths.batches)
    }
    rows: list[tuple[BatchManifest, int, int, int, Counter[str]]] = []
    for batch_dir in sorted(
        (path for path in paths.batches.iterdir() if path.is_dir()), reverse=True
    ):
        manifest = load_manifest(batch_dir)
        if state and state not in manifest.states:
            continue
        if namespace != "all" and namespace not in manifest.namespaces:
            continue
        checkpoints = [
            checkpoint
            for checkpoint in load_checkpoints(batch_dir)
            if (not state or checkpoint.state == state)
            and (namespace == "all" or checkpoint.entity_namespace == namespace)
        ]
        if not checkpoints:
            continue
        summary = summaries[manifest.batch_id]
        if len(checkpoints) == summary.task_count and not state and namespace == "all":
            attempt_count = summary.attempt_count
            result_count = summary.result_count
            statuses = summary.statuses
        else:
            attempt_count = sum(len(item.attempts) for item in checkpoints)
            result_count = sum(
                len(attempt.results)
                for item in checkpoints
                for attempt in item.attempts
            )
            statuses = Counter(item.terminal_status for item in checkpoints)
        rows.append((manifest, len(checkpoints), attempt_count, result_count, statuses))
    scope = f" for {state}" if state else ""
    if not rows:
        return f"I haven't run any discovery searches{scope} matching those filters yet."
    shown = rows[:limit]
    if len(shown) == 1:
        manifest, task_count, attempt_count, result_count, status_counts = shown[0]
        completed = set(status_counts) == {"success"}
        outcome = "" if completed else f" ({_batch_status_phrase(status_counts)})"
        lines = [
            f"I {'completed' if completed else 'ran'} one recent discovery search "
            f"on {_human_date(_batch_date(manifest.batch_id))} and "
            f"{_batch_result_counts(result_count)} across "
            f"{_batch_search_counts(task_count, attempt_count)}{outcome}."
        ]
    else:
        if len(rows) > len(shown):
            lines = [
                f"Here are my {_count_phrase(len(shown))} most recent discovery "
                f"searches{scope} — I have {_count_phrase(len(rows))} stored in all:"
            ]
        else:
            lines = [
                f"Here are my {_count_phrase(len(shown))} most recent discovery "
                f"searches{scope}, newest first:"
            ]
        for index, item in enumerate(shown, start=1):
            manifest, task_count, attempt_count, result_count, status_counts = item
            completed = set(status_counts) == {"success"}
            outcome = "" if completed else f" ({_batch_status_phrase(status_counts)})"
            lines.append(
                f"{index}. On {_human_date(_batch_date(manifest.batch_id))} I "
                f"{'completed' if completed else 'ran'} "
                f"{_batch_search_counts(task_count, attempt_count)} and "
                f"{_batch_result_counts(result_count)}{outcome}."
            )
    lines.append(
        "These are raw search results, not reviewed sources — a completed search "
        "doesn't mean a source was reviewed or added."
    )
    if state:
        lines.append(
            "Want me to show the newest leads, or list the sources we've reviewed?"
        )
    else:
        lines.append(
            "Want me to show the newest leads, or break these searches down by state?"
        )
    return "\n".join(lines)


def _render_summary(paths: DiscoveryStatusPaths, state: str) -> str:
    """Render a compact catalog, evidence, access, integration, and queue summary."""
    entries = _scope(load_catalog(paths.catalog), state)
    checks = [
        check
        for check in load_discovery_checks(paths.checks)
        if not state or check.state == state
    ]
    catalog_ids = {entry.source_id for entry in entries}
    reviewed_ids = {check.research_key for check in checks} & catalog_ids
    access = Counter(entry.access_mode.value for entry in entries)
    integration = Counter(entry.integration_status.value for entry in entries)
    validated_batch_ids = {
        summary.batch_id for summary in validate_stored_batches(paths.batches)
    }
    batch_count = sum(
        1
        for batch_dir in paths.batches.iterdir()
        if batch_dir.is_dir()
        and batch_dir.name in validated_batch_ids
        and (not state or state in load_manifest(batch_dir).states)
    )
    scope = f" for {state}" if state else " nationwide"
    # Items are semicolon-separated because several phrases contain their own comma.
    access_phrase = "; ".join(
        f"{value:,} {_ACCESS_MODE_WORDS.get(key, _safe_text(key))}"
        for key, value in sorted(access.items(), key=lambda item: (-item[1], item[0]))
    )
    stage_phrase = "; ".join(
        f"{value:,} {_INTEGRATION_WORDS.get(key, _safe_text(key))}"
        for key, value in sorted(
            integration.items(), key=lambda item: (-item[1], item[0])
        )
    )
    batch_word = "batch" if batch_count == 1 else "batches"
    lines = [
        f"Source discovery status{scope}:",
        f"- {len(entries):,} candidate sources catalogued",
        f"- {len(reviewed_ids):,} reviewed by hand so far",
        f"- {len(checks):,} pages checked with saved evidence",
        f"- {batch_count:,} raw search {batch_word} stored",
        f"- How we'd reach them: {access_phrase}",
        f"- How far along they are: {stage_phrase}",
        "Research coverage:",
    ]
    lines.extend(_render_coverage(paths, state, "all").splitlines()[1:-1])
    lines.append(
        "This is our research list, not the leads themselves — raw results, "
        "reviewed candidates, and live feeds are separate things."
    )
    return "\n".join(lines)


def source_inventory_status(
    view: str = "summary",
    state: str = "",
    namespace: str = "all",
    limit: int = 10,
    paths: DiscoveryStatusPaths | None = None,
) -> str:
    """Return one safe read-only Slack answer from validated discovery evidence."""
    selected_view = view.strip().lower() or "summary"
    selected_state = state.strip().upper()
    selected_namespace = namespace.strip().lower() or "all"
    if selected_view not in ALLOWED_VIEWS:
        return (
            f"ERROR: unsupported source inventory view '{_safe_text(selected_view)}'."
        )
    if selected_state and selected_state not in STATE_NAMES.values():
        return "ERROR: state must be a valid two-letter US state or DC code."
    if selected_namespace not in ALLOWED_NAMESPACES:
        return "ERROR: unsupported source inventory namespace."
    if not 1 <= limit <= 25:
        return "ERROR: source inventory limit must be between 1 and 25."
    evidence_paths = paths or DiscoveryStatusPaths()
    try:
        if selected_view == "coverage":
            return _render_coverage(evidence_paths, selected_state, selected_namespace)
        if selected_view == "reviewed_sources":
            return _render_reviewed_sources(
                evidence_paths, selected_state, selected_namespace, limit
            )
        if selected_view == "recent_batches":
            return _render_recent_batches(
                evidence_paths, selected_state, selected_namespace, limit
            )
        return _render_summary(evidence_paths, selected_state)
    except (OSError, ValueError):
        return "ERROR: validated source discovery evidence is unavailable."


def slack_source_status_reply(
    user_text: str, thread_context: list[str] | None = None
) -> str | None:
    """Return a deterministic Slack reply, bypassing all network-capable tools."""
    request = parse_status_request(user_text, thread_context)
    if request is None:
        return None
    if request.paid_execution_requested:
        return (
            "I can't start a new discovery run from here — paid discovery runs are "
            "disabled in Slack until they have a separate admin approval workflow. "
            "What I can do right now: show the discovery status, research coverage "
            "for any state, the sources we've reviewed, or the results of recent "
            "searches — just ask."
        )
    return source_inventory_status(
        view=request.view,
        state=request.state,
        namespace=request.namespace,
        limit=request.limit,
    )
