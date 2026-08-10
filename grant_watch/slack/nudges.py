"""Proactive follow-ups: Grant chasing work that was started and left unfinished.

THE HONESTY BUDGET IS THE HARD PART. Silence in a Slack thread is not evidence that
nobody acted — the rep may have phoned the district from the car, or done the work in
Salesforce by hand. So a nudge never says "you didn't follow up". It says what Grant
observed IN ITS OWN RECORDS, and then asks. The difference between those two sentences
is the whole of rule 1 in a message that goes to a team channel.

EVERY NUDGE IS A THREADED REPLY, never a new channel post. That is a product choice
and a schema choice at once: it puts the nudge where the work already is, it lets a
reply re-enter a live Grant thread for free, and it means a nudge needs neither a
`posts` row nor a `proactive_daily_slots` claim — both of which carry CHECK constraints
that would reject a new kind and require rebuilding a table with live foreign-key
children.

ONE NUDGE PER SUBJECT, EVER. Enforced by the schema, not by this worker. Grant has no
evidence anyone read the first one, so a second is nagging; a rep who has deliberately
parked something should not be asked about it again every morning.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .. import capability_asks, db, reminders, roster, territory
from ..migrations_nudges import NUDGE_SUBJECT_KINDS
from ..presentation import display_entity_name
from .drip import PT as BUSINESS_TZ
from .drip import in_window
from . import nudge_variants

# Bumping this re-opens every subject for one more nudge under the new rules. It is
# part of the schema's uniqueness key precisely so that is a deliberate act.
POLICY_VERSION = "nudge-v1"

# A nudge is a phone notification for whoever is mentioned, so it is capped even
# though it does not consume the daily card slot.
MAX_NUDGES_PER_DAY = 2
MAX_NUDGES_PER_TARGET_PER_DAY = 1
MIN_GAP = timedelta(hours=4)

# How long after the work stalls Grant waits, and how long before it gives up. A
# nudge about something from three weeks ago is noise, not help.
GRACE = {
    "crm_preview_expired": timedelta(hours=1),
    "crm_batch_blocked": timedelta(days=1),
    "crm_batch_partial": timedelta(days=2),
    "card_unengaged": timedelta(days=2),
    # No grace. The other kinds wait to see whether a human quietly finishes the work
    # anyway; there is nothing to wait for here, because the event being reported is
    # that Grant itself gained an ability.
    "capability_now_available": timedelta(0),
    # A manager hears about it only after the rep has had a fair run at it: the
    # rep's own follow-up lands at 2 days, so 4 gives them two clear business days
    # to answer before anyone else is told. Escalating sooner turns a nudge into
    # telling on a colleague.
    "card_escalated": timedelta(days=4),
    # A day is long enough that someone who simply got pulled away has had a chance
    # to come back on their own, and short enough that the thread is still live.
    "thread_abandoned": timedelta(days=1),
}

# Kinds delivered as a DIRECT MESSAGE rather than a threaded reply. Everything else
# in this worker is a reply in the thread the work lives in; an escalation is about
# someone else's silence and does not belong in the channel where they can see it
# being reported.
DM_KINDS = frozenset({"card_escalated"})

# The wordings a follow-up may use. Two, deliberately: enough to learn which reads
# better, few enough that every one is written and reviewed by a person rather than
# generated. See slack/nudge_variants.py for how one is chosen and measured.
VARIANTS = ("a", "b")
# How long a subject stays worth mentioning. Five days made the eligible window only
# THREE days wide (grace takes the first two), which had a consequence nobody
# intended: every subject that accumulated while this feature was switched off aged
# past it before the feature could ever look at them. Measured on production the day
# it shipped, 28 of 36 due subjects were ALREADY unreachable — the follow-up worker
# could not work the very backlog it exists to work, and would have reported "nothing
# to follow up on" while a fortnight of abandoned previews sat there. Fourteen days is
# still recent enough that a rep recognises what is being asked about, and wide enough
# that a one-a-day cap can actually drain a queue.
DROP_AFTER = timedelta(days=14)

# A BIGGER CONSTANT IS NOT ALWAYS THE FIX. Widening the window above rescues stalled
# work, but it cannot rescue a queue that STOPPED BEING FED: the playground's newest
# subject was already 22 days old when the 14-day window shipped, and the threshold
# needed to reach it grows by a day every day. That is a receding target, and the
# temptation is to keep raising the number until the test passes.
#
# `capability_now_available` sidesteps it by measuring from the right event instead.
# Its clock starts when the CAPABILITY shipped, not when the ask was made — see
# `_capability_asks`, which uses `available_since` as the stall time. "You asked me in
# July to email you those leads and I couldn't — I can now" is exactly as true four
# months later as it was the next morning, so the ask's age is simply not what
# staleness means for this kind. No special horizon is needed once the clock is
# anchored correctly.

# Suppression reasons that are FACTS ABOUT THE SUBJECT and will never stop being
# true. Only these may be written to the ledger, because that write is permanent:
# the uniqueness key retires the subject under this policy version forever.
PERMANENT_SUPPRESSIONS = frozenset(
    {"stale", "resolved_since_queued", "engaged_since_queued", "lead_parked"}
)


@dataclass(frozen=True)
class NudgeCandidate:
    """One piece of unfinished work, with the evidence its wording rests on."""

    subject_kind: str
    subject_id: str
    audience: str
    target_slack: str
    anchor_ts: str
    stalled_at: datetime
    observed: dict[str, Any]

    @property
    def due_at(self) -> datetime:
        """When this becomes worth mentioning."""
        return self.stalled_at + GRACE.get(self.subject_kind, timedelta(days=1))

    @property
    def drop_after(self) -> datetime:
        """After this it is stale; Grant drops it rather than posting late."""
        return self.stalled_at + DROP_AFTER


def _parse(value: object) -> datetime | None:
    """Parse a stored ISO timestamp, returning None rather than guessing."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _abandoned_previews(conn: sqlite3.Connection) -> list[NudgeCandidate]:
    """Previews a human was shown and never clicked.

    `state='ready'` past `expires_at` means exactly that Grant offered a button and
    the offer lapsed — NOT that the underlying work is undone. The wording downstream
    is careful about that distinction.
    """
    rows = conn.execute(
        """SELECT id,action_type,workspace,channel,thread_ts,requested_by,expires_at
             FROM crm_actions
            WHERE state='ready' AND expires_at IS NOT NULL"""
    ).fetchall()
    out: list[NudgeCandidate] = []
    for row in rows:
        expires = _parse(row["expires_at"])
        if expires is None:
            continue
        out.append(
            NudgeCandidate(
                subject_kind="crm_preview_expired",
                subject_id=str(row["id"]),
                audience=str(row["channel"] or ""),
                target_slack=str(row["requested_by"] or ""),
                anchor_ts=str(row["thread_ts"] or ""),
                stalled_at=expires,
                observed={
                    "action_type": str(row["action_type"] or ""),
                    "expires_at": str(row["expires_at"] or ""),
                },
            )
        )
    return out


def _stalled_batches(conn: sqlite3.Connection) -> list[NudgeCandidate]:
    """Campaign batches that stopped for a human and never restarted."""
    rows = conn.execute(
        """SELECT id,channel,thread_ts,requested_by,state,updated_at,unique_org_count
             FROM crm_campaign_batches
            WHERE state IN ('blocked_resolution','partial_by_user')"""
    ).fetchall()
    out: list[NudgeCandidate] = []
    for row in rows:
        stalled = _parse(row["updated_at"])
        if stalled is None:
            continue
        kind = (
            "crm_batch_blocked"
            if str(row["state"]) == "blocked_resolution"
            else "crm_batch_partial"
        )
        out.append(
            NudgeCandidate(
                subject_kind=kind,
                subject_id=str(row["id"]),
                audience=str(row["channel"] or ""),
                target_slack=str(row["requested_by"] or ""),
                anchor_ts=str(row["thread_ts"] or ""),
                stalled_at=stalled,
                observed={"organizations": int(row["unique_org_count"] or 0)},
            )
        )
    return out


def _unengaged_cards(conn: sqlite3.Connection, now: datetime) -> list[NudgeCandidate]:
    """Cards that drew no reply, no reaction, and no CRM action.

    Grant knows only what happened in Slack and in its own tables. That is why the
    message says "nothing has come back HERE", never "nobody followed up".
    """
    rows = conn.execute(
        """SELECT p.id,p.channel,p.ts,p.posted_at,p.lead_id,
                  l.entity_name,l.status,l.state,l.source,l.amount,
                  s.slack_user_id AS snapshot_tagged
             FROM posts p
             LEFT JOIN leads l ON l.id=p.lead_id
             LEFT JOIN rich_card_snapshots s ON s.id=p.snapshot_id
            WHERE p.lead_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM engagement e WHERE e.post_id=p.id)
            ORDER BY p.id DESC LIMIT 60"""
    ).fetchall()
    out: list[NudgeCandidate] = []
    for row in rows:
        posted = _parse(row["posted_at"])
        if posted is None or posted > now:
            continue
        # WHO DID THE CARD ACTUALLY TAG? Recomputed through the SAME gate the card
        # used, so the follow-up can only name a rep the card itself named. Using
        # `owner_for_state` alone would tag people on cards that went out untagged,
        # because an inferred state can own a territory but may never tag a human.
        # PREFER WHAT THE CARD ACTUALLY RECORDED. A rich card routes through the
        # Salesforce call owner, then the account owner, then the opportunity owner,
        # and only THEN territory — so recomputing from the state alone can name a
        # different person entirely. Telling a manager "this went to X and nothing
        # came back" about someone who was never asked is the worst thing this
        # feature could do, so the persisted value wins and the recomputation is only
        # a fallback for legacy cards that carry no snapshot.
        tagged = str(row["snapshot_tagged"] or "")
        if not tagged and territory.state_is_verified(row["source"]):
            tagged = territory.owner_for_state(row["state"]) or ""
        observed = {
            "entity_name": str(row["entity_name"] or ""),
            "lead_status": str(row["status"] or ""),
            "lead_id": int(row["lead_id"] or 0),
            "amount_usd": int(row["amount"] or 0),
            "channel": str(row["channel"] or ""),
            "card_ts": str(row["ts"] or ""),
            "tagged_slack": tagged,
        }
        out.append(
            NudgeCandidate(
                subject_kind="card_unengaged",
                subject_id=str(row["id"]),
                audience=str(row["channel"] or ""),
                # A tagged card is one person's to answer; an untagged one belongs to
                # the channel and is still asked about without naming anybody.
                target_slack=tagged,
                anchor_ts=str(row["ts"] or ""),
                stalled_at=posted,
                observed=observed,
            )
        )
        manager = roster.manager_slack_id()
        # ESCALATION IS A SEPARATE SUBJECT so it carries its own grace, its own
        # one-shot key, and its own opt-out check. It only exists when a specific
        # person was asked and did not answer — an untagged card has nobody to
        # escalate ABOUT, and telling a manager "nobody replied" is not actionable.
        if tagged and manager and manager != tagged:
            out.append(
                NudgeCandidate(
                    subject_kind="card_escalated",
                    subject_id=str(row["id"]),
                    audience=manager,
                    target_slack=manager,
                    anchor_ts=str(row["ts"] or ""),
                    stalled_at=posted,
                    observed=observed,
                )
            )
    return out


def _capability_asks(conn: sqlite3.Connection) -> list[NudgeCandidate]:
    """Asks Grant refused for want of a feature that now exists.

    `stalled_at` is `available_since` — WHEN THE CAPABILITY SHIPPED, not when the
    person asked. That is what makes this kind work without a special staleness
    horizon: the thing worth reporting is the change in what Grant can do, so the
    clock starts there. Anchoring it to the ask date instead would have made every
    historical ask permanently stale on the day it was recorded.
    """
    rows = conn.execute(
        """SELECT * FROM capability_asks
            WHERE state='open' AND available_since IS NOT NULL
            ORDER BY asked_at"""
    ).fetchall()
    out: list[NudgeCandidate] = []
    for row in rows:
        shipped = _parse(row["available_since"])
        if shipped is None:
            continue
        asked_at = _parse(row["asked_at"])
        out.append(
            NudgeCandidate(
                subject_kind="capability_now_available",
                subject_id=str(row["id"]),
                audience=str(row["audience"]),
                target_slack=str(row["slack_user"] or ""),
                anchor_ts=str(row["thread_ts"] or ""),
                stalled_at=shipped,
                observed={
                    "ask_text": str(row["ask_text"] or ""),
                    "capability": str(row["capability"] or ""),
                    "correction": str(row["correction"] or ""),
                    "asked_on": asked_at.astimezone(BUSINESS_TZ).strftime("%-d %B")
                    if asked_at
                    else "",
                    "evidence_url": str(row["evidence_url"] or ""),
                },
            )
        )
    return out


def _abandoned_threads(conn: sqlite3.Connection) -> list[NudgeCandidate]:
    """Conversations where Grant demonstrably failed to answer, and nobody came back.

    THE SIGNAL IS GRANT'S OWN ADMISSION, not an inference about the human. A receipt
    reaches `needs_reconciliation` when the turn's action or its final message did not
    complete — the "I'm having trouble thinking right now" replies, the messages
    truncated mid-word, the turns that produced nothing at all. Reading it this way
    means the follow-up says "I didn't get back to you", which Grant can prove, rather
    than "you didn't finish", which it cannot: the rep may well have gone and done the
    work by hand.

    Only the LATEST receipt in a thread qualifies. If the person sent anything
    afterwards they came back on their own, and there is nothing to apologise for.
    """
    rows = conn.execute(
        """SELECT r.event_id,r.channel,r.thread_ts,r.slack_user,r.received_at,r.error
             FROM slack_event_receipts r
            WHERE r.state='needs_reconciliation'
              AND r.reviewed_at IS NULL
              AND r.thread_ts IS NOT NULL
              AND r.slack_user IS NOT NULL
              AND NOT EXISTS (
                    SELECT 1 FROM slack_event_receipts later
                     WHERE later.channel=r.channel
                       AND later.thread_ts=r.thread_ts
                       AND later.received_at>r.received_at)
            ORDER BY r.received_at DESC LIMIT 40"""
    ).fetchall()
    out: list[NudgeCandidate] = []
    for row in rows:
        received = _parse(row["received_at"])
        if received is None:
            continue
        out.append(
            NudgeCandidate(
                subject_kind="thread_abandoned",
                subject_id=str(row["event_id"]),
                audience=str(row["channel"] or ""),
                target_slack=str(row["slack_user"] or ""),
                anchor_ts=str(row["thread_ts"] or ""),
                stalled_at=received,
                observed={"error": str(row["error"] or "")},
            )
        )
    return out


def candidates(conn: sqlite3.Connection, now: datetime) -> list[NudgeCandidate]:
    """Every piece of unfinished work Grant can honestly ask about, oldest first."""
    found = (
        _abandoned_previews(conn)
        + _stalled_batches(conn)
        + _unengaged_cards(conn, now)
        + _capability_asks(conn)
        + _abandoned_threads(conn)
    )
    ready = [
        item
        for item in found
        if item.subject_kind in NUDGE_SUBJECT_KINDS
        and item.anchor_ts
        and item.audience
        and item.due_at <= now
    ]
    return sorted(ready, key=lambda item: item.stalled_at)


def suppress_reason(
    conn: sqlite3.Connection, candidate: NudgeCandidate, now: datetime
) -> str:
    """Why this nudge must NOT be sent, or '' when it may go.

    Re-checked immediately before the send, inside the reservation, so a subject that
    resolved while it sat in the queue produces silence rather than a false claim.
    """
    if now > candidate.drop_after:
        return "stale"
    if db.channel_guard(conn, candidate.audience) is not None:
        return "channel_guard_active"
    if candidate.target_slack and reminders.is_opted_out(
        conn, candidate.target_slack, scope="nudges"
    ):
        return "opted_out"
    if candidate.subject_kind == "crm_preview_expired":
        row = conn.execute(
            "SELECT state FROM crm_actions WHERE id=?", (candidate.subject_id,)
        ).fetchone()
        if row is None or str(row["state"]) != "ready":
            return "resolved_since_queued"
    if candidate.subject_kind.startswith("crm_batch"):
        row = conn.execute(
            "SELECT state FROM crm_campaign_batches WHERE id=?", (candidate.subject_id,)
        ).fetchone()
        if row is None or str(row["state"]) not in {
            "blocked_resolution",
            "partial_by_user",
        }:
            return "resolved_since_queued"
    if candidate.subject_kind == "capability_now_available":
        row = conn.execute(
            "SELECT state FROM capability_asks WHERE id=?", (candidate.subject_id,)
        ).fetchone()
        if row is None or str(row["state"]) != "open":
            return "resolved_since_queued"
        # THE OFFER MUST BE LIVE WHERE THE MESSAGE LANDS, not merely shipped in code.
        # Reopening an ask apologises for a promise Grant did not keep and then makes
        # a new one; if RESEND_API_KEY is missing on the droplet, that new promise
        # fails the moment she says yes — a second broken promise to the same person
        # in the same thread. Transient, because setting the variable fixes it.
        if not _capability_is_live(str(candidate.observed.get("capability") or "")):
            return "capability_not_ready"
    # BOTH card kinds, not just the channel one. They are produced from the SAME post
    # row and the same observations, so a guard written for one is a guard the other
    # needs. Gating on the kind LABEL rather than the subject let a lead the rep had
    # explicitly marked not_relevant be suppressed in the channel and STILL DM the
    # manager that it had gone unanswered — the highest-consequence message in the
    # system, about a colleague, saying something untrue.
    if candidate.subject_kind in {"card_unengaged", "card_escalated"}:
        if _card_was_acted_on(conn, candidate):
            return "engaged_since_queued"
        # A lead a human deliberately parked is not unfinished work.
        if str(candidate.observed.get("lead_status") or "") in {
            "dead",
            "snoozed",
            "contacted",
            "not_relevant",
        }:
            return "lead_parked"
    return ""


def _card_was_acted_on(conn: sqlite3.Connection, candidate: NudgeCandidate) -> bool:
    """Whether anybody engaged with this card, by ANY route Grant can see.

    `engagement` records replies and reactions, but a BUTTON CLICK writes nowhere
    near it: the rich-card buttons write `rich_card_actions` (joined through a
    snapshot on lead_id, not on the post) and a Salesforce approval writes
    `crm_actions` in the card's own thread. Reading only `engagement` counted a card
    whose button somebody had pressed as completely ignored — and then chased them
    about it, and told their manager.
    """
    post_id = int(candidate.subject_id)
    if conn.execute(
        "SELECT 1 FROM engagement WHERE post_id=? LIMIT 1", (post_id,)
    ).fetchone():
        return True
    lead_id = int(candidate.observed.get("lead_id") or 0)
    if (
        lead_id
        and conn.execute(
            """SELECT 1 FROM rich_card_actions a
             JOIN rich_card_snapshots s ON s.id=a.snapshot_id
            WHERE s.lead_id=? LIMIT 1""",
            (lead_id,),
        ).fetchone()
    ):
        return True
    card_ts = str(candidate.observed.get("card_ts") or "")
    return bool(
        card_ts
        and conn.execute(
            "SELECT 1 FROM crm_actions WHERE thread_ts=? LIMIT 1", (card_ts,)
        ).fetchone()
    )


def _sent_today(
    conn: sqlite3.Connection, audience: str, now: datetime
) -> list[sqlite3.Row]:
    """Nudges already reserved or delivered in this Pacific day for one channel."""
    local = now.astimezone(BUSINESS_TZ)
    start = datetime.combine(local.date(), time.min, BUSINESS_TZ)
    return list(
        conn.execute(
            """SELECT target_slack,reserved_at FROM followup_nudges
                WHERE audience=? AND state IN ('reserved','delivered','unknown')
                  AND reserved_at>=?""",
            (audience, start.astimezone(timezone.utc).isoformat()),
        )
    )


def pacing_reason(
    conn: sqlite3.Connection,
    candidate: NudgeCandidate,
    now: datetime,
    *,
    force: bool = False,
) -> str:
    """Why this nudge must wait, or '' when the caps allow it now.

    `force` skips ONLY the business-hours window — the operator override for "send
    it now", used to exercise this path outside a weekday. It deliberately does not
    touch the one-shot rule, the suppression re-checks, or the daily caps: those are
    what stop a nudge being wrong or being nagging, and an override that skipped them
    would be testing something other than the real behaviour. Same shape as the drip's
    force, which was fixed today for exactly the opposite reason.
    """
    if not force and not in_window(now):
        return "outside business hours"
    today = _sent_today(conn, candidate.audience, now)
    if len(today) >= MAX_NUDGES_PER_DAY:
        return f"daily nudge cap reached ({MAX_NUDGES_PER_DAY})"
    if (
        candidate.target_slack
        and sum(
            1 for row in today if str(row["target_slack"]) == candidate.target_slack
        )
        >= MAX_NUDGES_PER_TARGET_PER_DAY
    ):
        return "already nudged this person today"
    latest = max(
        (_parse(row["reserved_at"]) for row in today if _parse(row["reserved_at"])),
        default=None,
    )
    if latest is not None and now - latest < MIN_GAP:
        return "too soon after the last nudge"
    return ""


def build_message(candidate: NudgeCandidate, variant: str = "a") -> str:
    """One short, human line that is hard to ignore — and still only claims what
    Grant actually observed.

    Length is the whole point. "Scottsbluff Public School still needs follow-up in
    Salesforce" is easy to scroll past; a direct question with a name in it is not.
    Reps were not replying to Grant, so these are written the way a colleague would
    poke you — brief, a little wry, and always ending in something answerable with
    one word. What they must never do is assert what a person did or did not do:
    Grant cannot see a phone call, so it reports its own records and then asks.
    """
    mention = f"<@{candidate.target_slack}> " if candidate.target_slack else ""
    if candidate.subject_kind == "crm_preview_expired":
        if variant == "b":
            return (
                f"{mention}that Salesforce approval expired before it was clicked — "
                "nothing was written. Shall I rebuild it?"
            )
        return (
            f"{mention}that Salesforce approval timed out before anyone hit the "
            "button, so nothing got written. Want me to rebuild it? 🙂"
        )
    if candidate.subject_kind == "crm_batch_blocked":
        count = candidate.observed.get("organizations", 0)
        if variant == "b":
            return (
                f"{mention}{count} organizations on this one are waiting on a call "
                "from you about how to match them. Shall I add the rest without them?"
            )
        return (
            f"{mention}still stuck on this one — {count} organizations need a call "
            "on how to match them. Want me to skip those and add the rest?"
        )
    if candidate.subject_kind == "crm_batch_partial":
        if variant == "b":
            return (
                f"{mention}the unmatched ones from this batch never made it into the "
                "campaign. Want me to try them again?"
            )
        return (
            f"{mention}we only added the ones I could match here — the rest never "
            "made it. Want me to have another go at them?"
        )
    if candidate.subject_kind == "capability_now_available":
        return _capability_message(candidate, mention, variant)
    if candidate.subject_kind == "card_escalated":
        return _escalation_message(candidate, mention, variant)
    if candidate.subject_kind == "thread_abandoned":
        if variant == "b":
            return (
                f"{mention}I dropped the ball on this one and never got you an "
                "answer. Want me to have another go?"
            )
        return (
            f"{mention}I never got you a proper answer on this one, and it looks like "
            "it stalled there. Want me to pick it back up?"
        )
    entity = display_entity_name(str(candidate.observed.get("entity_name") or ""))
    subject = entity or "that lead"
    if mention:
        # The card named this person, so the follow-up asks THEM rather than the room.
        # Addressing the channel about a card that pinged one rep produced a
        # follow-up nobody owned, which is how nine cards drew no reply at all.
        if variant == "b":
            # Leads with the MONEY rather than the silence. Which of these gets
            # answered more often is exactly what the variant ledger measures.
            amount = int(candidate.observed.get("amount_usd") or 0)
            money = f"${amount:,} " if amount > 0 else ""
            return (
                f"{mention}{money}{subject} is still sitting here — want me to find "
                "you a contact for it?"
            )
        return (
            f"{mention}still nothing back on {subject} — though that's only what I "
            "can see here. Want me to find a contact, or shall I drop it?"
        )
    if variant == "b":
        # The untagged wording needed its own alternate too. Without one, the ledger
        # recorded two labels carrying the SAME sentence — and because the whole
        # live queue is untagged cards, `choose` would have declared a winner from
        # pure noise after eight sends. That is the superstition this module's own
        # docstring says it exists to prevent.
        return (
            f"{subject} is still unclaimed. Shall I track down a contact for it, or "
            "let it go?"
        )
    return (
        f"Anyone want {subject}? Nothing's come back here and I've got no activity "
        "logged on it — though that's only what I can see. I can find a contact or "
        "drop it."
    )


# What Grant can now do, phrased as the offer it is. Keyed by capability so the
# sentence stays tied to the thing that actually shipped.
_CAPABILITY_OFFER = {
    "email_results": "I can email you a list now — want me to send it?",
    "campaign_load": (
        "I can build the Salesforce campaign now and add them for you — want me to?"
    ),
    "reminders": (
        "I can hold on to that now and come back to you — want me to set it up?"
    ),
    "contact_supplied": (
        "I can record what you tell me now, tagged as coming from you — want to "
        "give it to me again?"
    ),
}


# The variant-b opener: leads with the capability instead of the apology. Hand
# written per capability, because assembling it from the variant-a fragments
# produced duplicated words and messages that never asked anything.
_CAPABILITY_HEADLINE = {
    "email_results": "I can email you that list now — want it?",
    "campaign_load": "I can build the Salesforce campaign now — want me to?",
    "reminders": "I can hold on to that for you now — want me to set it up?",
    "contact_supplied": "I can record what you tell me now — want to give it again?",
}


def _capability_message(
    candidate: NudgeCandidate, mention: str, variant: str = "a"
) -> str:
    """Reopen an ask Grant had to refuse, quoting the person back to themselves.

    The quote is the evidence. This message makes a claim about something a named
    colleague said weeks ago, and the honest way to make that claim is to show the
    words rather than summarise them — a paraphrase that drifts is Grant putting
    words in someone's mouth, which is rule 1 pointed at a person instead of a lead.
    """
    asked = str(candidate.observed.get("ask_text") or "").strip()
    when = str(candidate.observed.get("asked_on") or "").strip()
    offer = _CAPABILITY_OFFER.get(
        str(candidate.observed.get("capability") or ""),
        "I can do that now — want me to?",
    )
    # Long asks are trimmed at a word boundary; the permalink in the ledger keeps the
    # full message one click away, so nothing is lost by not pasting all of it.
    if len(asked) > 160:
        asked = asked[:160].rsplit(" ", 1)[0] + "…"
    opener = f"back on {when}," if when else "a while back,"
    quoted = f'you asked: "{asked}".' if asked else "you asked me for this."
    # A correction REPLACES "I couldn't do it then". That sentence is true but
    # incomplete where Grant did not merely fail — it said the thing was handled.
    # Reporting only the capability gap would quietly omit the broken promise, which
    # is rule 1 applied to Grant's own conduct rather than to a lead.
    correction = str(candidate.observed.get("correction") or "").strip()
    admission = correction or "I couldn't do it then."
    if variant == "b":
        # LEADS WITH WHAT CHANGED rather than with the apology. Both wordings carry
        # the same quote and the same admission — which one a person actually answers
        # is the question the ledger exists to settle, and it cannot settle anything
        # while both labels carry one sentence.
        #
        # Written out rather than assembled from the variant-a pieces: reordering
        # those fragments produced "I can email you a list now now" and a message
        # that ended without asking anything. A wording a person will read is worth
        # writing by hand.
        headline = _CAPABILITY_HEADLINE.get(
            str(candidate.observed.get("capability") or ""),
            "Good news — I can do that one now.",
        )
        # Upper-case only the FIRST character. `.capitalize()` lower-cases everything
        # after it, which turned "back on 23 July" into "Back on 23 july".
        lead = opener[:1].upper() + opener[1:]
        return f"{mention}{headline} {lead} {quoted} {admission}"
    return f"{mention}{opener} {quoted} {admission} {offer}"


def run(
    client: WebClient | None,
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> str:
    """Deliver at most ONE nudge per invocation, reserving before Slack is called.

    Ordering is guard → suppression → pacing → reserve → post, and the reservation is
    committed BEFORE the Slack call so a crash mid-send cannot produce a second nudge
    on the next tick. A dry run returns before any write.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not dry_run:
        # Close the loop on earlier sends BEFORE choosing this one's wording, so the
        # choice is made on the freshest evidence available.
        nudge_variants.mark_engagement(conn)
    for candidate in candidates(conn, current):
        already = conn.execute(
            """SELECT state FROM followup_nudges
                WHERE subject_kind=? AND subject_id=? AND policy_version=?""",
            (candidate.subject_kind, candidate.subject_id, POLICY_VERSION),
        ).fetchone()
        if already is not None:
            continue
        reason = suppress_reason(conn, candidate, current)
        if reason:
            # ONLY a permanent reason may be written. `_record` inserts the row whose
            # uniqueness key retires this subject forever, and two of the reasons are
            # transient: `channel_guard_active` is a Slack outage, `opted_out` can be
            # reversed. Recording those would mean one bad afternoon — or one rep
            # asking for quiet and later changing their mind — silently destroying
            # every pending follow-up in that channel, with nothing to show it had
            # happened. Measured on production, a single run during an outage would
            # have burned 22 subjects permanently.
            if not dry_run and reason in PERMANENT_SUPPRESSIONS:
                _record(conn, candidate, current, state="suppressed", reason=reason)
            continue
        waiting = pacing_reason(conn, candidate, current, force=force)
        if waiting:
            return f"skip: {waiting}"
        variant = nudge_variants.choose(conn, candidate.subject_kind, VARIANTS)
        text = build_message(candidate, variant)
        if dry_run:
            return f"[dry-run] would nudge {candidate.subject_kind} ({variant}): {text}"
        nudge_id = _record(
            conn, candidate, current, state="reserved", reason=None, variant=variant
        )
        if client is None:
            return "skip: no Slack client configured"
        try:
            if candidate.subject_kind in DM_KINDS:
                # A DM has no thread to reply into, so the card is linked instead.
                # The permalink is fetched from Slack rather than assembled from a
                # guessed workspace URL — a broken link in an escalation is worse
                # than no link, because the whole message is "go look at this".
                link = _permalink(
                    client,
                    str(candidate.observed.get("channel") or ""),
                    str(candidate.observed.get("card_ts") or ""),
                )
                response = client.chat_postMessage(
                    channel=candidate.audience,
                    text=f"{text}\n{link}" if link else text,
                )
            else:
                response = client.chat_postMessage(
                    channel=candidate.audience,
                    thread_ts=candidate.anchor_ts,
                    text=text,
                )
        except SlackApiError as exc:
            code = str(exc.response.get("error") or "")
            # A missing thread is permanent; anything else may or may not have
            # landed, and a nudge is never blind-retried.
            state = "suppressed" if code == "thread_not_found" else "unknown"
            _finish(conn, nudge_id, state=state, error=code)
            return f"nudge failed ({code})"
        except Exception as exc:  # noqa: BLE001 — ambiguity is preserved, not retried
            _finish(conn, nudge_id, state="unknown", error=type(exc).__name__)
            return f"nudge ambiguous ({type(exc).__name__})"
        ts = str(response.get("ts") or "")
        _finish(conn, nudge_id, state="delivered", error=None, slack_ts=ts)
        if candidate.subject_kind == "capability_now_available":
            # We came back to them about it, so the ask is dealt with. Without this
            # the register grows forever and `close()` had no caller at all.
            capability_asks.close(conn, int(candidate.subject_id))
        return f"nudged {candidate.subject_kind} in {candidate.audience}"
    return "skip: nothing to follow up on"


def _record(
    conn: sqlite3.Connection,
    candidate: NudgeCandidate,
    now: datetime,
    *,
    state: str,
    reason: str | None,
    variant: str = "",
) -> str:
    """Persist the reservation (or the suppression) before any Slack call."""
    nudge_id = uuid.uuid4().hex
    with conn:
        conn.execute(
            """INSERT INTO followup_nudges
                 (id,subject_kind,subject_id,audience,target_slack,anchor_ts,
                  policy_version,due_at,drop_after,state,suppress_reason,
                  observed_json,delivery_key,reserved_at,variant)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                nudge_id,
                candidate.subject_kind,
                candidate.subject_id,
                candidate.audience,
                candidate.target_slack,
                candidate.anchor_ts,
                POLICY_VERSION,
                candidate.due_at.isoformat(),
                candidate.drop_after.isoformat(),
                state,
                reason,
                json.dumps(candidate.observed, sort_keys=True),
                f"nudge:{candidate.subject_kind}:{candidate.subject_id}:{POLICY_VERSION}",
                now.isoformat(),
                variant or None,
            ),
        )
    return nudge_id


def _finish(
    conn: sqlite3.Connection,
    nudge_id: str,
    *,
    state: str,
    error: str | None,
    slack_ts: str = "",
) -> None:
    """Close out a reserved nudge with what actually happened."""
    with conn:
        conn.execute(
            """UPDATE followup_nudges
                  SET state=?,last_error=?,slack_ts=?,delivered_at=?
                WHERE id=?""",
            (
                state,
                error,
                slack_ts or None,
                datetime.now(timezone.utc).isoformat(),
                nudge_id,
            ),
        )


def _escalation_message(
    candidate: NudgeCandidate, mention: str, variant: str = "a"
) -> str:
    """Tell a manager one lead went unanswered — briefly, and without accusing anyone.

    Chase asked for this and asked for it SHORT. The care needed is in what it does
    not say: Grant sees Slack and its own tables, so "nothing's come back here" is
    true and "she never followed up" is not — the rep may have phoned the district
    from the car. Naming the money and the person is the point (it is what makes the
    message actionable), so the sentence around them has to be exact.
    """
    entity = display_entity_name(str(candidate.observed.get("entity_name") or ""))
    amount = int(candidate.observed.get("amount_usd") or 0)
    who = str(candidate.observed.get("tagged_slack") or "")
    money = f"${amount:,} " if amount > 0 else ""
    owner = f"<@{who}>" if who else "the territory rep"
    subject = entity or "a lead"
    if variant == "b":
        return (
            f"{mention}{money}{subject} has been sitting with {owner} and nothing's "
            "come back here — could well be handled offline. Shall I dig out a "
            "contact?"
        )
    return (
        f"{mention}heads up — {money}{subject} went to {owner} and nothing's come "
        "back here since. Could just be handled offline. Want me to find a contact "
        "and draft something?"
    )


def _permalink(client: WebClient, channel: str, message_ts: str) -> str:
    """Slack's own permalink for one message, or "" when it cannot be obtained.

    Asked of Slack rather than built from a workspace URL, because a hand-assembled
    link that 404s turns an escalation into a dead end. Any failure degrades to no
    link at all; the message still names the organization and the rep.
    """
    if not channel or not message_ts:
        return ""
    try:
        return str(
            client.chat_getPermalink(channel=channel, message_ts=message_ts).get(
                "permalink"
            )
            or ""
        )
    except Exception:  # noqa: BLE001 — a missing link must never block the message
        return ""


def _capability_is_live(capability: str) -> bool:
    """Whether a capability can actually be honoured in THIS environment right now.

    Shipping the code is not the same as the feature working: email needs a Resend
    key on the machine that runs the worker. Anything without a runtime dependency
    is live as soon as it is deployed.
    """
    if capability == "email_results":
        from ..notify import resend_client

        return resend_client.is_configured()
    return True
