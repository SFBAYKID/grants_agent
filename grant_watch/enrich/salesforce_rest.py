"""Honest interpretation of Salesforce REST payloads: errors and describe results.

Salesforce answers a rejected call with a body naming the exact cause. Places in
this codebase used to throw that body away and substitute a guess, and the guess
reached a sales rep as a confident sentence that was not true. These functions
exist so the gateway can only say what Salesforce actually said.

Two questions are kept apart on purpose, because conflating them was the second
bug found in this fix's own review:

- *Permanence* -- will asking again produce the same answer? That is provable from
  the error code, and Grant may assert it.
- *Cause* -- whose problem is it? That is NOT always knowable. Salesforce returns
  `INVALID_FIELD: No such column 'X'` identically for "this user has no read on X"
  and "X does not exist in this org", and a `MALFORMED_QUERY` is Grant's own bug.
  Telling a rep to go to a Salesforce admin about Grant's syntax error is the same
  rule-1 failure as blaming an outage, pointed the other way.
"""

from __future__ import annotations

import re
from typing import Any, Iterable  # Salesforce REST response JSON is runtime-shaped.

import requests

#: Failures that a permission or schema change could clear. Read codes and write
#: codes both appear: `INVALID_FIELD_FOR_INSERT_UPDATE` is field-level security on
#: create, and `INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY` is exactly what
#: Salesforce returns when creating a CampaignMember on a Campaign you cannot edit
#: -- the most likely failure of the add-members step this fix exists to unblock.
ACCESS_ERROR_CODES = frozenset(
    {
        "INVALID_FIELD",
        "INVALID_TYPE",
        "INSUFFICIENT_ACCESS",
        "INSUFFICIENT_ACCESS_OR_READONLY",
        "INVALID_FIELD_FOR_INSERT_UPDATE",
        "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY",
        "INVALID_CROSS_REFERENCE_KEY",
        "CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY",
    }
)

#: Permanent, but OURS. A rep must never be sent to an admin for one of these.
GRANT_DEFECT_ERROR_CODES = frozenset({"MALFORMED_QUERY"})

#: Everything retrying cannot clear.
PERMANENT_ERROR_CODES = ACCESS_ERROR_CODES | GRANT_DEFECT_ERROR_CODES

#: Salesforce echoes the failing SOQL, a caret and a row/column marker ahead of the
#: actual sentence. It is noise to a rep, it is most of the length, and on the
#: campaign-create path the echoed query inlines a colleague's email address and
#: Salesforce username -- so stripping it removes a Slack PII exposure as well as
#: making the cause survive truncation.
_SOQL_ECHO = re.compile(r"^.*?ERROR at Row:\d+:Column:\d+\s*", re.DOTALL)


def _error_items(response: requests.Response) -> list[dict[str, Any]]:
    """Return the error objects in a failed response body, whatever its shape."""
    try:
        body: Any = response.json()
    except ValueError:
        return []
    items = body if isinstance(body, list) else [body]
    return [item for item in items if isinstance(item, dict)]


def error_codes(response: requests.Response) -> set[str]:
    """Return every Salesforce error code in the body.

    Reads `statusCode` as well as `errorCode`: the composite sObject collections
    endpoint -- which is how every batched Lead and CampaignMember create is
    submitted -- reports per-record failures under `statusCode`, so a reader
    looking only for `errorCode` classifies none of them.
    """
    codes: set[str] = set()
    for item in _error_items(response):
        for key in ("errorCode", "statusCode"):
            value = item.get(key)
            if value:
                codes.add(str(value))
    return codes


def salesforce_error_detail(response: requests.Response) -> str:
    """Return Salesforce's own code and message for a failed REST response.

    `raise_for_status()` discards the body, so the only text reaching a rep was
    `400 Client Error: Bad Request for url: ...`. That looks like an outage, which
    is why Grant told a rep on 2026-09-21 that Salesforce was having "a temporary
    hiccup" and to try again -- twice, for an error that was permanent.
    """
    parts: list[str] = []
    for item in _error_items(response):
        code = str(item.get("errorCode") or item.get("statusCode") or "")
        message = _SOQL_ECHO.sub("", str(item.get("message") or "")).strip()
        joined = ": ".join(piece for piece in (code, message) if piece)
        if joined:
            parts.append(joined)
    if parts:
        return "; ".join(parts)[:400]
    for item in _error_items(response):
        oauth = [
            str(item.get(key))
            for key in ("error", "error_description")
            if item.get(key)
        ]
        if oauth:
            return ": ".join(oauth)[:400]
    return response.text.strip()[:200] or "(no response body)"


def is_permanent_error(response: requests.Response) -> bool:
    """Whether asking again would produce the same answer."""
    return bool(error_codes(response) & PERMANENT_ERROR_CODES)


def is_access_error(response: requests.Response) -> bool:
    """Whether a Salesforce permission or schema change could clear this."""
    return bool(error_codes(response) & ACCESS_ERROR_CODES)


def is_grant_defect(response: requests.Response) -> bool:
    """Whether this failure is Grant's own bug and no admin can fix it."""
    return bool(error_codes(response) & GRANT_DEFECT_ERROR_CODES)


def permanence_note(response: requests.Response) -> str:
    """One sentence stating permanence, and cause only where it is knowable."""
    if is_grant_defect(response):
        return (
            "This is a defect in Grant's own request, not anything about this "
            "Salesforce org; retrying will not help"
        )
    if is_access_error(response):
        return (
            "Retrying will not help: most likely field-level security for this "
            "integration user, or a field that does not exist in this org"
        )
    return ""


def raise_for_salesforce_status(response: requests.Response) -> None:
    """Raise with Salesforce's explanation instead of a bare status line.

    Raises `requests.HTTPError` so every existing `except requests.RequestException`
    site keeps catching it; only the message improves. The permanence sentence goes
    FIRST, ahead of Salesforce's own text, because every rep-facing caller truncates
    and a note appended after a 500-character body is a note nobody ever reads.
    """
    if response.status_code < 400:
        return
    detail = salesforce_error_detail(response)
    note = permanence_note(response)
    body = f"{note}. {detail}" if note else detail
    raise requests.HTTPError(
        f"HTTP {response.status_code} from Salesforce: {body}",
        response=response,
    )


def create_failure_detail(response: requests.Response) -> str:
    """Rep-facing text for a create that Salesforce rejected without raising.

    The create primitives return a result object rather than raising, so they never
    passed through `raise_for_salesforce_status` and reported a bare status plus raw
    JSON. That is the path every Campaign, Lead, CampaignMemberStatus and
    CampaignMember create runs through -- the rep's next click after the one this
    fix unblocked.
    """
    note = permanence_note(response)
    detail = salesforce_error_detail(response)
    return (
        f"HTTP {response.status_code}: {note}. {detail}"
        if note
        else (f"HTTP {response.status_code}: {detail}")
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
            "That is most likely field-level security rather than a Grant bug: "
            "a Salesforce admin granting read access on those fields would fix it."
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
    left the cause to be guessed. The judgement belongs next to the code that can
    make it, and it has to travel with the error, because by the time the model sees
    a tool result the response is long gone.

    Returns an empty string for anything that might genuinely be transient, so a
    real outage is still described as one.
    """
    if isinstance(exc, PicklistNotReadable):
        return (
            "Grant cannot READ that picklist, so it does not know which values are "
            "valid. Do NOT ask the rep which values their org has enabled and do "
            "NOT ask them to try another value. Name the field and say a Salesforce "
            "admin needs to grant read access on it."
        )
    response = getattr(exc, "response", None)
    if response is None or not is_permanent_error(response):
        return ""
    if is_grant_defect(response):
        return (
            "This is a bug in Grant, not in this Salesforce org. Do NOT suggest "
            "retrying and do NOT send the rep to a Salesforce admin. Say plainly "
            "that Grant built a bad request and that it has been reported."
        )
    return (
        "Retrying cannot clear this. Do NOT suggest waiting, trying again, or a "
        "different name or spelling. Tell the rep what is blocked and that it needs "
        "a Salesforce admin to grant this integration user access to that field or "
        "object, and that you have flagged it."
    )
