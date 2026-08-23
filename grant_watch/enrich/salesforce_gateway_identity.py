"""Pure identity, validation and header helpers for the Salesforce writer.

Split out of `salesforce_campaign_gateway` at the 1000-line cap (rule 4). Everything
here is a PURE function of its arguments -- no HTTP client, no token, no gateway
state -- so a change in this file cannot alter what a confirmed write actually sends.
That is why this is the right seam rather than a convenient one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any  # Salesforce REST record JSON is runtime-shaped.
from urllib.parse import urlparse


_ID_PREFIXES = {
    "Campaign": "701",
    "Lead": "00Q",
    "Contact": "003",
    "Account": "001",
    "Opportunity": "006",
    "User": "005",
    "Organization": "00D",
    "Note": "002",
}


def validate_record_id(record_id: str, expected_sobject: str) -> str:
    """Validate a 15/18-character Salesforce ID and its object prefix."""
    clean = record_id.strip()
    expected_prefix = _ID_PREFIXES.get(expected_sobject)
    if expected_prefix is None:
        raise ValueError(f"unsupported Salesforce object '{expected_sobject}'")
    if (
        len(clean) not in (15, 18)
        or not clean.isalnum()
        or not clean.startswith(expected_prefix)
    ):
        raise ValueError(f"not a valid {expected_sobject} Salesforce ID")
    return clean


def parse_identity_url(identity_url: str) -> tuple[str, bool]:
    """Return (organization_id, is_sandbox) from Salesforce's token identity URL.

    WHY THIS REPLACED A QUERY. `verify_write_scope` used to establish org identity by
    running `SELECT ... FROM Organization`, which needs a READ PERMISSION on an object
    a least-privilege integration user is not granted -- so hardening the account broke
    every write at the gate (measured in production 2026-08-23: INVALID_TYPE).

    The identity URL is returned by the OAuth token endpoint itself, over TLS to the
    configured My Domain, and looks like:

        https://login.salesforce.com/id/00D41000002jIQ8EAM/005iL000001OsUvQAK
        https://test.salesforce.com/id/00DVC00000A6xPR2AZ/00541000001dACEAA2

    It is Salesforce ASSERTING who this token belongs to, alongside the token itself --
    the same trust boundary, and it cannot be withheld by a sharing rule or an object
    permission. `test.salesforce.com` means a sandbox; `login.salesforce.com` means
    production. Anything else is unrecognised and must fail closed rather than be
    guessed at, because guessing wrong here means writing to the WRONG ORG.
    """
    parsed = urlparse(identity_url)
    host = (parsed.hostname or "").lower()
    parts = [segment for segment in parsed.path.split("/") if segment]
    if parsed.scheme != "https" or len(parts) < 3 or parts[0] != "id":
        raise PermissionError(
            "Salesforce returned no usable identity URL for the writer token"
        )
    organization_id = parts[1]
    if host == "test.salesforce.com":
        sandbox = True
    elif host == "login.salesforce.com":
        sandbox = False
    else:
        raise PermissionError(
            f"Unrecognised Salesforce identity host {host!r}; refusing to infer "
            "whether this is production or a sandbox"
        )
    return organization_id, sandbox


def _conditional_headers(
    authorization_headers: dict[str, str], current: dict[str, Any]
) -> dict[str, str] | None:
    """Build Salesforce's optimistic-update header from the exact retrieved version.

    Salesforce documents ``If-Unmodified-Since`` for sObject-row PATCH requests and
    returns 412 when another actor changes the record after the supplied
    ``LastModifiedDate``. Refusing a missing or malformed date is safer than silently
    falling back to an unconditional write over a colleague's live CRM edit.
    """
    raw = str(current.get("LastModifiedDate") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return {
        **authorization_headers,
        "If-Unmodified-Since": format_datetime(
            parsed.astimezone(timezone.utc), usegmt=True
        ),
    }
