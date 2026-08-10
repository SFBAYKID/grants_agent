"""Deliver reminders that have come due, over Slack and/or email.

ORDERING IS RESERVE → SEND → RECORD, the same shape the drip and the nudge worker
use, and for the same reason: a reservation written after a successful send is
indistinguishable from no send at all if the process dies in between, and the next
run would deliver it again. The UNIQUE key on (reminder, occurrence, channel) is what
makes the reservation authoritative.

EACH CHANNEL IS RESERVED SEPARATELY so a Slack failure cannot suppress the email or
the other way round — a "both" reminder that half-succeeded should retry only the
half that failed, and only under the never-blind-retry rule below.

AMBIGUITY IS NEVER RETRIED. A timeout might mean the message landed. Sending again to
be sure is how a reminder becomes two reminders, so an indeterminate outcome is
recorded as `unknown` and left alone; the occurrence stays claimed. A missed reminder
is a smaller failure than a duplicate one, and unlike a duplicate it is visible.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import reminders
from .notify import resend_client
from .presentation import for_human
from .slack.search import NO_MATCH_PREFIX, search_leads

# One reminder per invocation, matching the nudge worker. The cron tick is every 30
# minutes, so a backlog drains steadily rather than arriving as a burst of pings.
MAX_PER_RUN = 1


def _render(reminder: reminders.Reminder) -> tuple[str, str]:
    """Re-run the frozen search and return (headline, body) for delivery.

    The search runs fresh at delivery time, so a weekly reminder reports this week's
    leads. When the spec is empty the reminder is a plain nudge about its subject and
    makes no claim about data at all.
    """
    kwargs = reminders.search_kwargs(reminder.search_spec)
    if not kwargs or set(kwargs) == {"limit"}:
        return (f"Reminder: {reminder.subject}", "")
    text, _artifact = search_leads(**kwargs)
    # A reminder posts this straight to a human with no model in between, so the
    # model-facing coaching has to come out. A live test in the playground posted
    # "Offer these to the user (with counts) and ask which to run" into a thread.
    body = for_human(text)

    # A REMINDER MUST NEVER DEAD-END. The first one Chase received read: "No grants
    # matched those filters. Nearby alternatives — without the state filter: 75
    # matches." — and stopped. His response was the right one: why would the user
    # need to know that? A message that reports a miss and offers nothing is a
    # message people learn to ignore, which is how Grant lost its own team.
    #
    # So when the frozen search is empty, actually GO AND GET the nearest real
    # leads rather than merely counting them, and hand over something workable.
    if body.startswith(NO_MATCH_PREFIX):
        broadened = {k: v for k, v in kwargs.items() if k != "state"}
        if broadened != kwargs:
            wider, _ = search_leads(**broadened)
            wider = for_human(wider)
            if not wider.startswith(NO_MATCH_PREFIX):
                where = str(kwargs.get("state", "")).upper()
                return (
                    f"Reminder: {reminder.subject}",
                    f"Nothing new in {where} today, but here's the closest I've "
                    f"got:\n\n{wider}",
                )
    return (f"Reminder: {reminder.subject}", body)


def _slack_text(reminder: reminders.Reminder, headline: str, body: str) -> str:
    """The threaded Slack message, in Grant's voice.

    It always says how to make it stop. A proactive message that does not tell you
    how to switch it off is the reason people mute bots.
    """
    who = f"<@{reminder.requested_by_slack}> " if reminder.requested_by_slack else ""
    parts = [f"{who}you asked me to remind you about {reminder.subject}."]
    if body:
        parts.append(body)
    # ALWAYS END WITH SOMETHING THEY CAN DO. A reminder that stops after reporting
    # is a dead end, and a dead end is a message nobody answers.
    parts.append(
        "Want me to put these on a Salesforce campaign, or find contacts for the "
        "best few?"
        if body
        else "Want me to pick this up now?"
    )
    if reminder.cadence != "once":
        parts.append(
            f'This repeats {reminder.cadence} — say "stop reminding me" any time '
            "and I'll drop it."
        )
    return "\n\n".join(parts)


def _email_body(reminder: reminders.Reminder, body: str) -> str:
    """The plain-text email, which has to stand alone away from the thread."""
    lines = [
        f"You asked me to remind you about {reminder.subject}.",
        "",
        body or "(No results to show — the reminder was for the subject itself.)",
        "",
        "— Grant, Monarch Connected",
        'Reply in Slack and say "stop reminding me" to switch these off.',
    ]
    return "\n".join(lines)


def _deliver_slack(
    client: WebClient | None,
    conn: sqlite3.Connection,
    reminder: reminders.Reminder,
    text: str,
) -> str:
    """Post one reminder into its original thread, honouring the reservation."""
    delivery_id = reminders.reserve(conn, reminder, "slack")
    if delivery_id is None:
        return "slack: already delivered"
    if client is None:
        reminders.complete(
            conn, delivery_id, state="failed", detail="no Slack client configured"
        )
        return "slack: no client configured"
    try:
        response = client.chat_postMessage(
            channel=reminder.audience, thread_ts=reminder.thread_ts, text=text
        )
    except SlackApiError as exc:
        code = str(exc.response.get("error") or "")
        reminders.complete(conn, delivery_id, state="failed", detail=code)
        return f"slack failed ({code})"
    except Exception as exc:  # noqa: BLE001 — ambiguity is preserved, never retried
        reminders.complete(
            conn, delivery_id, state="unknown", detail=type(exc).__name__
        )
        return f"slack ambiguous ({type(exc).__name__})"
    reminders.complete(
        conn, delivery_id, state="delivered", slack_ts=str(response.get("ts") or "")
    )
    return "slack: delivered"


def _deliver_email(
    conn: sqlite3.Connection, reminder: reminders.Reminder, subject: str, body: str
) -> str:
    """Email one rostered rep, refusing rather than guessing at an address."""
    delivery_id = reminders.reserve(conn, reminder, "email")
    if delivery_id is None:
        return "email: already delivered"
    try:
        outcome = resend_client.send_to_rep(reminder.requested_by_slack, subject, body)
    except (
        resend_client.EmailNotConfigured,
        resend_client.RecipientNotAllowed,
        ValueError,
    ) as exc:
        reminders.complete(conn, delivery_id, state="failed", detail=str(exc)[:200])
        return f"email refused ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001 — a send may still have happened
        reminders.complete(
            conn, delivery_id, state="unknown", detail=type(exc).__name__
        )
        return f"email ambiguous ({type(exc).__name__})"
    reminders.complete(
        conn,
        delivery_id,
        state="delivered",
        email_id=outcome.email_id,
        recipient=outcome.recipient,
    )
    return f"email: delivered to {outcome.recipient}"


def run(
    client: WebClient | None,
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> str:
    """Deliver at most MAX_PER_RUN due reminders, reserving before every send."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sent = 0
    results: list[str] = []
    for reminder in reminders.due(conn, current):
        if sent >= MAX_PER_RUN:
            break
        # Belt and braces: `create` refuses for someone already opted out, and
        # `set_optout` cancels their reminders — but a reminder created before an
        # opt-out and a race between the two both end here rather than in an inbox.
        if reminders.is_opted_out(conn, reminder.requested_by_slack):
            reminders.cancel(conn, reminder.reminder_id, reminder.requested_by_slack)
            results.append(f"#{reminder.reminder_id}: cancelled (opted out)")
            continue
        # ONE BAD REMINDER MUST NOT STOP THE REST. `due()` returns oldest-first, so
        # anything that raises here sits permanently at the head of the queue and
        # every later reminder — including ones with no search at all — never fires
        # again. `create` now validates the spec, but this loop must survive whatever
        # a pre-existing row, a schema change, or a source outage throws at it.
        try:
            headline, body = _render(reminder)
        except Exception as exc:  # noqa: BLE001 — isolate the item, keep the queue
            conn.execute(
                "UPDATE reminders SET state='failed', last_error=?, updated_at=? "
                "WHERE id=?",
                (
                    f"{type(exc).__name__}: {exc}"[:300],
                    datetime.now(timezone.utc).isoformat(),
                    reminder.reminder_id,
                ),
            )
            conn.commit()
            results.append(f"#{reminder.reminder_id}: failed ({type(exc).__name__})")
            continue
        if dry_run:
            results.append(
                f"[dry-run] #{reminder.reminder_id} via {reminder.deliver_via}: "
                f"{_slack_text(reminder, headline, body)[:200]}"
            )
            sent += 1
            continue
        if reminder.deliver_via in {"slack", "both"}:
            results.append(
                f"#{reminder.reminder_id} "
                + _deliver_slack(
                    client, conn, reminder, _slack_text(reminder, headline, body)
                )
            )
        if reminder.deliver_via in {"email", "both"}:
            results.append(
                f"#{reminder.reminder_id} "
                + _deliver_email(conn, reminder, headline, _email_body(reminder, body))
            )
        reminders.advance(conn, reminder)
        sent += 1
    if not results:
        return "skip: nothing due"
    return "; ".join(results)
