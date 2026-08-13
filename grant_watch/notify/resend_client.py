"""Send email to Monarch reps through Resend — and to nobody else.

WHY THIS IS SHAPED THIS WAY. A rep asked Grant to email her a list of leads and the
thread dead-ended, so Grant needs to be able to send mail. But an agent that can send
mail is one prompt away from emailing a school administrator, which Constitution rule
10 reserves for the human-approved Persequor path.

THE GUARDRAIL IS THE SIGNATURE, NOT A CHECK. `send_to_rep` accepts a SLACK USER ID and
resolves it through the reviewed roster itself. There is no parameter anywhere in this
module that accepts a caller-supplied address, so no prompt, tool argument, or scraped
page can steer a message to an outside mailbox — the capability simply is not exposed.
A validation check could be argued past; a missing parameter cannot. This is the same
reasoning as the `_ACTION_PRODUCING_TOOLS` boundary in slack/tools.py: make the unsafe
thing unrepresentable rather than forbidden.

Everything fails closed. No API key, no from-address, or an unrecognised Slack id all
produce a refusal that says which one it was, and nothing is sent.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Any, Final, Sequence

import requests

from .. import roster

API_URL: Final = "https://api.resend.com/emails"
# Resend rejects an unverified sender outright, so this stays required rather than
# defaulted — a plausible-looking default would turn a config mistake into a silent
# failure at send time instead of a clear refusal here.
FROM_ENV: Final = "RESEND_FROM_EMAIL"
KEY_ENV: Final = "RESEND_API_KEY"
TIMEOUT_SECONDS: Final = 20
# Resend's documented total is 40 MB after Base64 encoding. This raw-content cap
# leaves room for encoding expansion, JSON, and the message body.
MAX_RAW_ATTACHMENT_BYTES: Final = 28 * 1024 * 1024
MAX_ATTACHMENTS: Final = 1
_SAFE_XLSX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}\.xlsx\Z")


class EmailNotConfigured(RuntimeError):
    """Raised when Resend credentials or the sender identity are absent."""


class RecipientNotAllowed(PermissionError):
    """Raised when a Slack id does not resolve to a reviewed roster mailbox."""


@dataclass(frozen=True)
class EmailOutcome:
    """The complete, honest result of one attempted send."""

    sent: bool
    recipient: str
    email_id: str
    detail: str


@dataclass(frozen=True)
class EmailAttachment:
    """One bounded local attachment ready for Resend's Base64 API field."""

    filename: str
    content: bytes


def _attachment_payloads(
    attachments: Sequence[EmailAttachment],
) -> list[dict[str, str]]:
    """Validate the one generated workbook and encode it for Resend."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("email may contain only one generated spreadsheet")
    total_bytes = 0
    payloads: list[dict[str, str]] = []
    for attachment in attachments:
        filename = os.path.basename(attachment.filename)
        if not _SAFE_XLSX_NAME.fullmatch(filename):
            raise ValueError("email attachment must have a safe .xlsx filename")
        if not isinstance(attachment.content, bytes) or not attachment.content:
            raise ValueError("email attachment must contain generated workbook bytes")
        total_bytes += len(attachment.content)
        payloads.append(
            {
                "filename": filename,
                "content": base64.b64encode(attachment.content).decode("ascii"),
            }
        )
    if total_bytes > MAX_RAW_ATTACHMENT_BYTES:
        raise ValueError("email attachments exceed the safe Resend size limit")
    return payloads


def is_configured() -> bool:
    """Whether a send could be attempted at all, without attempting one."""
    return bool(_env(KEY_ENV)) and bool(_env(FROM_ENV))


def _env(name: str) -> str:
    """Read one environment value, treating whitespace-only as absent."""
    return (os.environ.get(name) or "").strip()


def recipient_for(slack_user: object) -> str:
    """Resolve a Slack id to its reviewed roster mailbox, or refuse.

    Test mode is honoured here rather than at the transport, so the redirect applies
    to every caller and shows up in the returned outcome the rep is shown.
    """
    resolved = roster.email_for_slack(slack_user)
    if not resolved:
        raise RecipientNotAllowed(
            "that Slack user is not on the reviewed Monarch roster, so there is no "
            "mailbox I am allowed to send to"
        )
    # ITS OWN VARIABLE, DELIBERATELY. `OUTREACH_TEST_EMAIL` exists to stop PROSPECT
    # outreach reaching a school administrator during testing — a different risk with
    # a different owner. Sharing it meant every rep's own results were silently
    # redirected to the test mailbox: measured on production, mail for Kerry, Nelly
    # and Jocelyn all landed elsewhere while `email_results` told them "Sent it to
    # …". Clearing the shared variable to fix that would have un-protected Persequor
    # at the same time, which is why one switch for two risks was the bug.
    override = _env("RESEND_TEST_EMAIL")
    return override or resolved


def send_to_rep(
    slack_user: object,
    subject: str,
    text_body: str,
    *,
    html_body: str = "",
    attachments: Sequence[EmailAttachment] = (),
    dry_run: bool = False,
    session: Any | None = None,
) -> EmailOutcome:
    """Email one rostered Monarch rep, resolving the address from their Slack id.

    `session` exists so tests can drive the real request-building and response-parsing
    code against a stub transport instead of skipping it; production passes nothing.
    """
    recipient = recipient_for(slack_user)
    if not subject.strip():
        raise ValueError("an email needs a subject")
    if not text_body.strip():
        raise ValueError("an email needs a body")
    attachment_payloads = _attachment_payloads(attachments)
    if dry_run:
        return EmailOutcome(
            sent=False,
            recipient=recipient,
            email_id="",
            detail="dry run — nothing was sent",
        )
    key = _env(KEY_ENV)
    sender = _env(FROM_ENV)
    if not key or not sender:
        missing = KEY_ENV if not key else FROM_ENV
        raise EmailNotConfigured(f"{missing} is not set, so email is switched off")

    payload: dict[str, object] = {
        "from": sender,
        "to": [recipient],
        "subject": subject.strip(),
        "text": text_body,
    }
    if html_body.strip():
        payload["html"] = html_body
    if attachment_payloads:
        payload["attachments"] = attachment_payloads
    http = session or requests
    response = http.post(
        API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        # Resend puts the actionable reason in the body (unverified domain, invalid
        # sender). Surfacing the status alone would send an operator hunting.
        raise requests.HTTPError(
            f"Resend refused the send ({response.status_code}): {response.text[:300]}"
        )
    body = response.json() if response.content else {}
    email_id = str(body.get("id") or "")
    return EmailOutcome(
        sent=True,
        recipient=recipient,
        email_id=email_id,
        detail="delivered to Resend",
    )
