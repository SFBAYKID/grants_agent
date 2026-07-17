"""Deterministic confirmation plans for Grant's natural-language lead searches.

The model may suggest filters, but this module preserves explicit human constraints
and prevents a first search or materially corrected search from running unseen.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Any  # Search arguments are constrained later by typed tool schemas.

from .source_status import _state_from_text

SEARCH_PLAN_MARKER = "Search plan:"


def search_plan_confirmed(user_text: str, thread_context: list[str] | None) -> bool:
    """Recognize an explicit reply to Grant's deterministic search-plan gate."""
    current = user_text.strip().lower()
    affirmative = bool(
        re.match(r"^(?:yes\b|yep\b|sure\b|go ahead\b|run it\b|do it\b)", current)
    )
    supplied_choice = bool(
        re.search(r"\b(?:top\s+\d+|all|excel|google sheet|here|thread)\b", current)
    )
    prior_plan = any(
        SEARCH_PLAN_MARKER.lower() in line.lower()
        for line in (thread_context or [])[-10:]
    )
    if prior_plan and _changes_prior_search_filters(user_text, thread_context):
        # A count/format choice can complete the existing plan, but a material filter
        # correction creates a new plan that the human must see before it executes.
        return False
    # Search approval is meaningful only after Grant displayed the exact plan. Contact
    # enrichment follow-ups use a separate with_contacts path and do not need this gate.
    return prior_plan and (affirmative or supplied_choice)


def _changes_prior_search_filters(
    user_text: str, thread_context: list[str] | None
) -> bool:
    """Return whether a follow-up materially changes Grant's last search plan."""
    prior = next(
        (
            line
            for line in reversed((thread_context or [])[-10:])
            if SEARCH_PLAN_MARKER.lower() in line.lower()
        ),
        "",
    )
    if not prior:
        return False
    current = basic_search_arguments(user_text)
    prior_values = {
        key: value.strip().lower()
        for key, value in re.findall(
            r"\b(location|organization|program|grade)=([^;.]+)", prior, re.IGNORECASE
        )
    }
    if not prior_values:
        # Current human format: "Search plan: IL · school · SVPP · gold[ · …]".
        marker_index = prior.lower().find(SEARCH_PLAN_MARKER.lower())
        summary = prior[marker_index + len(SEARCH_PLAN_MARKER) :]
        summary = summary.split(" — ", 1)[0].split(".", 1)[0]
        fields = [field.strip().lower() for field in summary.split("·")]
        if len(fields) >= 4:
            prior_values = {
                "location": fields[0],
                "organization": fields[1],
                "program": fields[2],
                "grade": fields[3],
            }
    comparisons = {
        "state": "location",
        "org_type": "organization",
        "program": "program",
        "grade": "grade",
    }
    return any(
        str(current[key]).lower() != prior_values.get(plan_key, "")
        for key, plan_key in comparisons.items()
        if key in current
    )


_DATE_FIELD_PHRASES = {
    "award_received": "announced",
    "discovered": "discovered",
    "opportunity_open": "application window opens",
    "opportunity_close": "application window closes",
    "solicitation_posted": "solicitation posted",
    "response_due": "responses due",
    "spend_start": "spend window starts",
    "spend_end": "spend window ends",
}

_RECORD_KIND_PHRASES = {
    "award": "awards only",
    "funding_opportunity": "funding opportunities only",
    "solicitation": "solicitations only",
}


def search_confirmation(arguments: dict[str, Any], user_text: str) -> str:
    """Render the proposed read-only query as one scannable human line.

    Format contract (parsed by _changes_prior_search_filters): the four core
    filters follow the marker as "location · org · program · grade"; everything
    after " — " or the first period is presentation only."""
    state = str(arguments.get("state") or "anywhere")
    org_type = str(arguments.get("org_type") or "any org type")
    program = str(arguments.get("program") or "any program")
    grade = str(arguments.get("grade") or "any grade")
    fields = [state, org_type, program, grade]
    date_field = str(arguments.get("date_field") or "")
    date_from = str(arguments.get("date_from") or "")
    date_to = str(arguments.get("date_to") or "")
    if date_field and (date_from or date_to):
        phrase = _DATE_FIELD_PHRASES.get(date_field, date_field)
        fields.append(f"{phrase} {date_from or 'any time'} to {date_to or 'any time'}")
    for key, render in (
        ("record_kind", lambda v: _RECORD_KIND_PHRASES.get(str(v), str(v))),
        ("amount_min", lambda v: f"amount at least ${v}"),
        ("amount_max", lambda v: f"amount at most ${v}"),
        ("enrollment_min", lambda v: f"enrollment at least {v}"),
        ("enrollment_max", lambda v: f"enrollment at most {v}"),
        ("city", lambda v: f"city {v}"),
        ("name_contains", lambda v: f"matching “{v}”"),
    ):
        value = arguments.get(key)
        if value not in (None, ""):
            fields.append(render(value))
    limit = arguments.get("limit")
    scope = str(arguments.get("result_scope") or "top_n")
    count_requested = bool(
        re.search(
            r"\b(?:one|two|three|four|five|ten|top\s+\d+|\d+|all|as many)\b",
            user_text.lower(),
        )
    )
    requested_thread = bool(
        re.search(r"\b(?:here|thread|in slack)\b", user_text.lower())
    )
    export_value = str(arguments.get("export") or "")
    plan = f"{SEARCH_PLAN_MARKER} " + " · ".join(fields) + "."
    chosen: list[str] = []
    if count_requested:
        if scope == "all":
            chosen.append("all matches")
        elif isinstance(limit, int):
            chosen.append(f"top {limit}")
    if export_value:
        chosen.append(
            {"excel": "Excel file", "google_sheet": "Google Sheet"}.get(
                export_value, export_value
            )
        )
    elif requested_thread:
        chosen.append("listed here in the thread")
    if chosen:
        plan += " I'll bring back the " + ", ".join(chosen) + "."
    questions: list[str] = []
    if not count_requested:
        questions.append("How many — top 5, top 10, or all?")
    if not export_value and not requested_thread:
        questions.append("And here in the thread, an Excel file, or a Google Sheet?")
    if questions:
        return plan + " " + " ".join(questions)
    return plan + " Reply yes and I’ll run it."


def finalize_unconfirmed_search_plan(
    output: dict[str, Any], search_confirmed: bool
) -> dict[str, Any]:
    """Prevent a captured first-turn plan from claiming that its search is running."""
    reply = str(output.get("reply") or "").strip()
    marker_index = reply.lower().find(SEARCH_PLAN_MARKER.lower())
    if search_confirmed or marker_index < 0:
        return output
    # Friendly preambles make the durable marker unreliable for the next human turn.
    # Keep the plan itself and discard only text before its explicit marker.
    reply = reply[marker_index:]
    reply = re.sub(
        r"\s*(?:running (?:that|it) now|i(?:'|’)m running (?:that|it) now)\.?\s*$",
        "",
        reply,
        flags=re.IGNORECASE,
    ).rstrip()
    if "reply yes" not in reply.lower():
        reply += " Reply yes and I’ll run it."
    output["reply"] = reply
    return output


def repair_missing_search_plan(
    user_text: str, output: dict[str, Any], search_confirmed: bool
) -> dict[str, Any]:
    """Rebuild a canonical plan when the model asks shape questions without a marker."""
    reply = str(output.get("reply") or "")
    lowered_reply = reply.lower()
    lowered_user = user_text.lower()
    search_subject = bool(
        re.search(
            r"\b(?:grant|grants|lead|leads|award|awards|rfp|funding|"
            r"school|schools|district|districts|city|cities|county|counties)\b",
            lowered_user,
        )
    )
    search_request = bool(
        re.search(r"\b(?:find|show|give|list|export|need|want)\b", lowered_user)
        and search_subject
        and not re.search(
            r"\b(?:source discovery|source inventory|research coverage)\b", lowered_user
        )
    )
    asks_shape = "how many" in lowered_reply and bool(
        re.search(r"\b(?:excel|google sheet|thread|slack)\b", lowered_reply)
    )
    marker_present = SEARCH_PLAN_MARKER.lower() in lowered_reply
    temporal_filter = bool(
        re.search(
            r"\b(?:next|last|this|past|before|after|between|since|during|"
            r"deadline|window|month|week|year|days?)\b",
            lowered_user,
        )
    )
    if (
        search_confirmed
        or not search_request
        or not (asks_shape or marker_present)
        or temporal_filter
    ):
        return output
    arguments = basic_search_arguments(user_text)
    output["reply"] = search_confirmation(arguments, user_text)
    output["intent"] = "question"
    return output


def basic_search_arguments(user_text: str) -> dict[str, Any]:
    """Parse explicit filters for deterministic first-turn plan rendering."""
    lowered_user = user_text.lower()
    arguments: dict[str, Any] = {}
    state = _state_from_text(user_text)
    if state:
        arguments["state"] = state
    if re.search(r"\b(?:school|schools|district|districts)\b", lowered_user):
        arguments["org_type"] = "school"
    elif re.search(r"\b(?:city|cities|town|towns)\b", lowered_user):
        arguments["org_type"] = "city"
    elif re.search(r"\b(?:county|counties)\b", lowered_user):
        arguments["org_type"] = "county"
    for program in ("SVPP", "NSGP", "CSSGP", "STOP"):
        if re.search(rf"\b{program}\b", user_text, re.IGNORECASE):
            arguments["program"] = program
            break
    for grade in ("gold", "silver", "watch"):
        if re.search(rf"\b{grade}\b", lowered_user):
            arguments["grade"] = grade
            break
    if re.search(r"\b(?:rfp|rfps|solicitation|solicitations)\b", lowered_user):
        arguments["record_kind"] = "solicitation"
    elif re.search(r"\bgrants?\.gov\b|\bfunding opportunities?\b", lowered_user):
        arguments["record_kind"] = "funding_opportunity"
    elif re.search(r"\b(?:award|awards)\b", lowered_user):
        arguments["record_kind"] = "award"

    number_words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    count_match = re.search(
        r"\b(?:top|show|give|find|need|want|list|export)\s+"
        r"(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        lowered_user,
    )
    if count_match is not None:
        raw_count = count_match.group(1)
        arguments["limit"] = (
            int(raw_count) if raw_count.isdigit() else number_words[raw_count]
        )
        arguments["result_scope"] = "top_n"
    elif re.search(r"\b(?:all|as many as you can find)\b", lowered_user):
        arguments["result_scope"] = "all"

    if "google sheet" in lowered_user:
        arguments["export"] = "google_sheet"
    elif re.search(r"\bexcel\b", lowered_user):
        arguments["export"] = "excel"

    amount_match = re.search(
        r"\b(?:over|more than|at least|minimum(?: of)?)\s+"
        r"(?:\$([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*([km]))\b",
        lowered_user,
    )
    if amount_match is not None:
        raw_amount = amount_match.group(1) or amount_match.group(2)
        multiplier = {"k": 1_000, "m": 1_000_000}.get(amount_match.group(3) or "", 1)
        arguments["amount_min"] = float(raw_amount.replace(",", "")) * multiplier

    enrollment_match = re.search(
        r"\b(?:over|more than|at least|minimum(?: of)?)\s+([\d,]+)\s+students?\b",
        lowered_user,
    )
    if enrollment_match is not None:
        enrollment = int(enrollment_match.group(1).replace(",", ""))
        if enrollment_match.group(0).startswith(("over", "more than")):
            enrollment += 1
        arguments["enrollment_min"] = enrollment
    date_range = _explicit_month_range(lowered_user)
    date_field = _date_field_from_text(lowered_user)
    if date_range is not None and date_field:
        arguments["date_field"] = date_field
        arguments["date_from"], arguments["date_to"] = date_range
    return arguments


def _explicit_month_range(user_text: str) -> tuple[str, str] | None:
    """Extract one explicit or relative calendar month without inventing a date."""
    today = date.today()
    relative = re.search(r"\b(last|this|next)\s+month\b", user_text)
    if relative is not None:
        month_index = today.year * 12 + today.month - 1
        month_index += {"last": -1, "this": 0, "next": 1}[relative.group(1)]
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
    else:
        names = {
            "january": 1,
            "jan": 1,
            "february": 2,
            "feb": 2,
            "march": 3,
            "mar": 3,
            "april": 4,
            "apr": 4,
            "may": 5,
            "june": 6,
            "jun": 6,
            "july": 7,
            "jul": 7,
            "august": 8,
            "aug": 8,
            "september": 9,
            "sep": 9,
            "october": 10,
            "oct": 10,
            "november": 11,
            "nov": 11,
            "december": 12,
            "dec": 12,
        }
        match = re.search(r"\b(" + "|".join(names) + r")\s+(20\d{2})\b", user_text)
        if match is None:
            return None
        month = names[match.group(1)]
        year = int(match.group(2))
    last_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def _date_field_from_text(user_text: str) -> str:
    """Map explicit human date semantics to the corresponding indexed field."""
    if re.search(r"\bdiscover(?:ed|y|ies)?\b", user_text):
        return "discovered"
    if re.search(r"\b(?:opportunit\w*|grants?\.gov)\b", user_text):
        if re.search(r"\b(?:clos\w*|deadline|due)\b", user_text):
            return "opportunity_close"
        if re.search(r"\b(?:open\w*|posted|start\w*)\b", user_text):
            return "opportunity_open"
    if re.search(r"\b(?:rfp|solicitation)\w*\b", user_text):
        if re.search(r"\b(?:deadline|due|respond|clos\w*)\b", user_text):
            return "response_due"
        if re.search(r"\b(?:posted|published|open\w*|start\w*)\b", user_text):
            return "solicitation_posted"
    if re.search(r"\bspend(?:ing)?\s+windows?\b", user_text):
        if re.search(r"\b(?:end\w*|expir\w*|clos\w*)\b", user_text):
            return "spend_end"
        if re.search(r"\b(?:start\w*|begin\w*|open\w*)\b", user_text):
            return "spend_start"
    return ""
