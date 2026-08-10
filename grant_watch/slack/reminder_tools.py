"""Grant's tool surface for reminders, opt-outs, and emailing a rep their results.

WHY THESE ARE SEPARATE TOOLS. "Remind me Friday", "what am I on the hook for", "stop"
and "just email it to me" are four different intents that a rep expresses in one
casual sentence each. Collapsing them into one parameterised tool made the model pick
wrong arguments under ambiguity; four narrow tools with obvious names do not.

NONE OF THESE TAKES AN EMAIL ADDRESS. The recipient is always resolved from the
requester's Slack id through the reviewed roster, inside `notify.resend_client`. A
tool that accepted an address would let a prompt — or a scraped page — aim Grant's
mail at an outside inbox, which is exactly the boundary Constitution rule 10 draws.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .. import db, reminders
from ..notify import resend_client
from .drip import PT as BUSINESS_TZ
from .search import search_leads

# A reminder in the past would fire on the next tick, which is never what someone
# means; the model is told to send a real future time and this catches the rest.
_MIN_LEAD_SECONDS = 60


def _parse_when(value: str) -> datetime:
    """Read a model-supplied time as Pacific unless it says otherwise.

    Reps say "Friday morning", and the model turns that into a local wall-clock time.
    Treating a naive timestamp as UTC would silently shift every reminder by seven or
    eight hours — the kind of bug that looks like flakiness rather than a timezone.
    """
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError("I need to know when to remind you")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "I couldn't read that date — give me a day and time, like 2026-08-14 09:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUSINESS_TZ)
    return parsed.astimezone(timezone.utc)


def reminder_set(
    args: dict[str, Any], requester_slack: str, channel: str, thread_ts: str
) -> str:
    """Schedule a reminder for the person who asked for it."""
    if not requester_slack:
        return "ERROR: I can't set a reminder without knowing who's asking."
    subject = str(args.get("subject", "")).strip()
    try:
        due = _parse_when(str(args.get("when", "")))
    except ValueError as exc:
        return f"ERROR: {exc}"
    if (due - datetime.now(timezone.utc)).total_seconds() < _MIN_LEAD_SECONDS:
        return "ERROR: that time has already passed — give me a time in the future."
    cadence = str(args.get("cadence", "once")).strip().lower() or "once"
    deliver_via = str(args.get("deliver_via", "slack")).strip().lower() or "slack"
    if deliver_via in {"email", "both"} and not resend_client.is_configured():
        # Say so BEFORE storing it. Accepting an email reminder that can never be
        # emailed would be a promise Grant knows it cannot keep.
        return (
            "ERROR: email isn't switched on for me yet, so I can only do this one in "
            "Slack. Want me to set it up that way instead?"
        )
    conn = db.connect()
    try:
        reminder_id = reminders.create(
            conn,
            requested_by_slack=requester_slack,
            audience=channel,
            thread_ts=thread_ts,
            subject=subject,
            due_at=due,
            cadence=cadence,
            deliver_via=deliver_via,
            search_spec=dict(args.get("search_spec") or {}),
        )
    except reminders.OptedOut:
        return (
            "ERROR: you'd asked me to stop sending follow-ups. Say you want them back "
            "on and I'll set this up."
        )
    except ValueError as exc:
        return f"ERROR: {exc}"
    finally:
        conn.close()
    local = due.astimezone(BUSINESS_TZ)
    repeat = "" if cadence == "once" else f", repeating {cadence}"
    return (
        f"Reminder #{reminder_id} set for {local:%A %-d %B at %-I:%M %p} Pacific"
        f"{repeat}, about: {subject}. Delivery: {deliver_via}."
    )


def reminder_list(requester_slack: str) -> str:
    """Show one person exactly what Grant is holding for them."""
    if not requester_slack:
        return "ERROR: I can't look that up without knowing who's asking."
    conn = db.connect()
    try:
        mine = reminders.for_user(conn, requester_slack)
        opted_out = reminders.is_opted_out(conn, requester_slack)
    finally:
        conn.close()
    if opted_out and not mine:
        return "You've got no reminders, and you've asked me not to send follow-ups."
    if not mine:
        return "You've got no reminders set."
    lines = [
        f"- #{item.reminder_id}: {item.subject} — "
        f"{item.next_due_at.astimezone(BUSINESS_TZ):%a %-d %b, %-I:%M %p} Pacific"
        + ("" if item.cadence == "once" else f" (repeats {item.cadence})")
        for item in mine
    ]
    return "Here's what I'm holding for you:\n" + "\n".join(lines)


def reminder_cancel(args: dict[str, Any], requester_slack: str) -> str:
    """Cancel one reminder, or every reminder this person has."""
    if not requester_slack:
        return "ERROR: I can't cancel anything without knowing who's asking."
    conn = db.connect()
    try:
        if bool(args.get("all", False)):
            stopped = reminders.cancel_all(conn, requester_slack)
            if not stopped:
                return "You didn't have any reminders running."
            return f"Stopped all {stopped} of your reminders."
        raw = args.get("reminder_id")
        if raw is None:
            return "ERROR: which reminder? Give me its number, or say all of them."
        done = reminders.cancel(conn, int(raw), requester_slack)
    finally:
        conn.close()
    if not done:
        return (
            "I couldn't cancel that one — either it isn't yours, or it already "
            "finished. Ask me to list them and we'll find the right one."
        )
    return f"Cancelled reminder #{int(raw)}."


def stop_followups(
    args: dict[str, Any], requester_slack: str, channel: str, thread_ts: str
) -> str:
    """Switch off proactive messages for this person, or switch them back on."""
    if not requester_slack:
        return "ERROR: I can't do that without knowing who's asking."
    conn = db.connect()
    try:
        if bool(args.get("resume", False)):
            reminders.clear_optout(conn, requester_slack)
            return "Done — I'll follow up with you again when there's something."
        scope = str(args.get("scope", "all")).strip().lower() or "all"
        cancelled = reminders.set_optout(
            conn,
            requester_slack,
            scope=scope,
            channel=channel,
            thread_ts=thread_ts,
            note=str(args.get("note", ""))[:200],
        )
    except ValueError as exc:
        return f"ERROR: {exc}"
    finally:
        conn.close()
    # Built from what the write ACTUALLY did. The old wording claimed cancelled
    # reminders unconditionally, so someone who asked to stop only the nudges was
    # told their reminders were off — and then kept receiving them.
    what = {
        "all": "I've stopped following up with you",
        "reminders": "I've stopped the reminders",
        "nudges": "I've stopped the proactive nudges — your reminders still run",
    }[scope]
    tail = f", and cancelled the {cancelled} you had running" if cancelled else ""
    return f"Done — {what}{tail}. Say the word if you want them back."


def email_results(args: dict[str, Any], requester_slack: str) -> str:
    """Email the requester a set of search results, right now.

    This is the fix for the ask that dead-ended in July: a rep said "just email me
    these" and Grant had no way to. The search is run here rather than trusting a
    caller-supplied body, so the email says what the database actually holds.
    """
    if not requester_slack:
        return "ERROR: I can't email results without knowing who's asking."
    if not resend_client.is_configured():
        return (
            "ERROR: email isn't switched on for me yet — no Resend key is set. I can "
            "post the list right here instead, or build you a spreadsheet."
        )
    spec = dict(args.get("search_spec") or {})
    subject = str(args.get("subject", "")).strip() or "Your Grant lead list"
    body, _artifact = search_leads(**reminders.search_kwargs(spec))
    try:
        outcome = resend_client.send_to_rep(
            requester_slack,
            subject,
            f"{body}\n\n— Grant, Monarch Connected",
        )
    except resend_client.RecipientNotAllowed:
        return (
            "ERROR: I don't have you on the reviewed Monarch roster, so I don't have "
            "an address I'm allowed to send to. Chase can add you."
        )
    except Exception as exc:  # noqa: BLE001 — reported honestly, never retried blind
        return f"ERROR: the send failed ({type(exc).__name__}). Nothing was sent."
    return f"Sent it to {outcome.recipient}."
