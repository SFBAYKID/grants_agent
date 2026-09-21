"""Honest interpretation of Salesforce REST payloads: errors and describe results.

Salesforce answers a rejected read with a body that names the exact cause. Two
places in this codebase used to throw that body away and substitute a guess, and
both guesses reached a sales rep as a confident sentence that was not true. These
functions exist so the gateway can only say what Salesforce actually said.
"""

from __future__ import annotations

from typing import Any, Iterable  # Salesforce REST response JSON is runtime-shaped.

import requests

#: Salesforce error codes that mean "this user may not see that", as opposed to
#: anything transient. Retrying one of these can never succeed, so a caller that
#: knows the difference can stop telling a human to try again in a minute.
PERMANENT_ACCESS_ERROR_CODES = frozenset(
    {
        "INVALID_FIELD",  # a field this user has no field-level read on
        "INVALID_TYPE",  # an object this user has no read access to at all
        "INSUFFICIENT_ACCESS",
        "INSUFFICIENT_ACCESS_OR_READONLY",
        "MALFORMED_QUERY",
    }
)


def salesforce_error_detail(response: requests.Response) -> str:
    """Return Salesforce's own errorCode and message for a failed REST response.

    Salesforce rejects a read for an inaccessible field with HTTP 400 and a body
    reading `INVALID_FIELD: No such column 'Type' on entity 'Campaign'`, which is
    both the diagnosis and the fix. `raise_for_status()` discards it, so the only
    text reaching a rep was `400 Client Error: Bad Request for url: ...`. That
    looks like an outage, which is why Grant told a rep on 2026-09-21 that
    Salesforce was having "a temporary hiccup" and to try again -- twice, for an
    error that was permanent and about permissions.

    The body is third-party shaped: a list of errors for the data APIs, a single
    object for OAuth. Anything unrecognized falls back to the raw text, bounded,
    because an ugly true string beats a tidy invented one.
    """
    try:
        body: Any = response.json()
    except ValueError:
        return response.text.strip()[:200] or "(no response body)"
    if isinstance(body, list):
        parts = [
            ": ".join(
                str(item.get(key)) for key in ("errorCode", "message") if item.get(key)
            )
            for item in body
            if isinstance(item, dict)
        ]
        detail = "; ".join(part for part in parts if part)
        if detail:
            return detail[:400]
    if isinstance(body, dict):
        parts = [
            str(body.get(key))
            for key in ("error", "error_description", "errorCode", "message")
            if body.get(key)
        ]
        if parts:
            return ": ".join(parts)[:400]
    return response.text.strip()[:200] or "(no response body)"


def is_permanent_access_error(response: requests.Response) -> bool:
    """Whether this failure is a permission fact rather than a passing outage."""
    try:
        body: Any = response.json()
    except ValueError:
        return False
    items = body if isinstance(body, list) else [body]
    return any(
        isinstance(item, dict)
        and str(item.get("errorCode") or "") in PERMANENT_ACCESS_ERROR_CODES
        for item in items
    )


def raise_for_salesforce_status(response: requests.Response) -> None:
    """Raise with Salesforce's explanation instead of a bare status line.

    Raises `requests.HTTPError` so every existing `except requests.RequestException`
    site keeps catching it; only the message improves.
    """
    if response.status_code < 400:
        return
    detail = salesforce_error_detail(response)
    if is_permanent_access_error(response):
        detail = (
            f"{detail} -- this is a Salesforce permission, not an outage, so "
            "retrying will not help"
        )
    raise requests.HTTPError(
        f"HTTP {response.status_code} from Salesforce: {detail}",
        response=response,
    )


class PicklistNotReadable(ValueError):
    """A picklist field Salesforce never showed us, so its values are unknown."""


def active_picklist_values(
    describe_body: dict[str, Any], field_names: Iterable[str]
) -> dict[str, set[str]]:
    """Return each field's active picklist values, or raise if one was not exposed.

    `describe` is filtered by the running user's field-level security: a field this
    integration user cannot read is ABSENT from the payload, not present-and-empty.
    The caller used to seed an empty set per field and fill it from whatever the
    payload happened to contain, so an invisible field and a field with no active
    values were the same answer -- and the refusal it produced, "Campaign Type
    'Other' is not active", was an assertion about the customer's Salesforce
    configuration that this code had no evidence for. An empty set rejects every
    value a human can name, which is exactly what a rep hit on 2026-09-21.

    Raising names the field and the permission to grant, which is the one thing
    that actually ends the loop.
    """
    wanted = tuple(field_names)
    found: dict[str, set[str]] = {}
    for field in describe_body.get("fields") or []:
        name = str(field.get("name") or "")
        if name in wanted:
            found[name] = {
                str(item.get("value"))
                for item in field.get("picklistValues") or []
                if item.get("active")
            }
    missing = [name for name in wanted if name not in found]
    if missing:
        raise PicklistNotReadable(
            "Salesforce did not expose "
            + "/".join(missing)
            + " to this integration user, so the valid values cannot be read. "
            "That is field-level security in Salesforce rather than a Grant bug: "
            "grant the integration user read access on those fields."
        )
    return found


def picklist_refusal(field: str, supplied: str, allowed: set[str]) -> str:
    """Refuse an inactive picklist value while naming the ones that would work.

    The old message said only that the supplied value was not active, so the only
    way to answer it was to guess. Chase watched a rep guess "Other", then "Event",
    then abandon the task (2026-09-21). The valid set is already in hand at the
    moment of the refusal, and withholding it is what turns a one-line correction
    into a dead end.
    """
    offered = ", ".join(sorted(allowed)) if allowed else "(none are active)"
    return (
        f"Campaign {field} '{supplied}' is not active in Salesforce. "
        f"Active {field} values are: {offered}"
    )


def permanent_failure_guidance(exc: BaseException) -> str:
    """Model guidance for a Salesforce failure that retrying can never clear.

    Grant told a rep a 400 was "a temporary hiccup with their API" and asked her to
    try again -- twice, then a third time with a different name (2026-09-21). It was
    not free-associating: the tool handed it `400 Client Error: Bad Request` and
    left the cause to be guessed. The judgement of whether a failure is permanent
    belongs next to the code that can actually make it, and the answer has to travel
    with the error, because by the time the model sees a tool result the response is
    long gone.

    Returns an empty string for anything that might genuinely be transient, so a
    real outage is still described as one.
    """
    if isinstance(exc, PicklistNotReadable):
        return (
            "This is a Salesforce field-level-security gap, not a configuration "
            "choice the rep made. Do NOT ask the rep which values their org has "
            "enabled and do NOT ask them to try another value. Say plainly that "
            "Grant cannot read that picklist, name the field, and say it needs a "
            "Salesforce admin to grant read access."
        )
    response = getattr(exc, "response", None)
    if response is None or not is_permanent_access_error(response):
        return ""
    return (
        "This failure is a Salesforce permission or configuration fact, not an "
        "outage. Do NOT suggest retrying, waiting, or trying a different name or "
        "spelling. Tell the rep plainly what is blocked, that it needs a Salesforce "
        "admin change rather than another attempt, and that you have flagged it."
    )
