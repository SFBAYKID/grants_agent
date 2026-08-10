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
import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .. import capability_asks, db, reminders, roster, territory
from ..migrations_nudges import NUDGE_SUBJECT_KINDS
from .drip import PT as BUSINESS_TZ
from .drip import in_window
from . import nudge_variants
from .nudge_messages import build_message

# Bumping this re-opens every subject for one more nudge under the new rules. It is
# part of the schema's uniqueness key precisely so that is a deliberate act.
POLICY_VERSION = "nudge-v1"

# A nudge is a phone notification for whoever is mentioned, so it is capped even
# though it does not consume the daily card slot.
MAX_NUDGES_PER_DAY = 2
MAX_NUDGES_PER_TARGET_PER_DAY = 1
MIN_GAP = timedelta(hours=4)

# WHEN a follow-up may land, Pacific. Grant is a cron job and a fixed schedule makes
# it read like one: a message that arrives at 09:15 every single weekday is
# furniture, and people stop seeing furniture. The daily card already solved this by
# drawing ONE target time per day inside a band; follow-ups now do the same.
#
# THE BAND IS COUPLED TO THE CRON AND MUST NOT OUTRUN IT. A slot drawn after the
# last tick of the day means "never": every tick logs `holding for today's slot`,
# nothing posts, and both lines read as routine. Caught immediately when this shipped
# against a cron that ran only at 09:15 and 14:15 — any slot after 14:15 was
# unreachable, so more than half the band silently meant silence.
#
# The cron is therefore `*/30 8-15 * * 1-5` (last tick 15:30). CHANGE THEM TOGETHER.
#
# The band ends at 14:30 rather than 15:00 for a SECOND reason, measured after the
# first version shipped: the recipient's own working-hours gate is `8 <= local < 18`,
# so for an Eastern rep 15:00 Pacific is 18:00 local and already refused. A slot
# drawn at the structural maximum would have been unreachable for Kerry — the same
# silent-hold class this band exists to avoid, arriving from the other end. 14:30 PT
# is 17:30 Eastern, which leaves margin for the tightest zone on the roster.
NUDGE_BAND_START_PT = time(8, 30)
NUDGE_BAND_END_PT = time(14, 30)

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
    # ONE DAY, AND IT NOW MEANS SOMETHING DIFFERENT THAN IT DID. The watchdog runs
    # every ten minutes and marks a receipt reviewed as soon as it repairs the
    # spinner, so on the happy path this subject is gone within half an hour and this
    # follow-up never fires. That reads like dead code and is not: the watchdog leaves
    # `reviewed_at` NULL whenever the Slack edit FAILS, which is precisely the case
    # where a person is still looking at "Thinking…" a day later.
    #
    # So this became the fallback for a repair that did not work, rather than the
    # primary path — a strictly better division of labour, arrived at by accident when
    # TOO_OLD was widened. Recorded because "it can never fire" is the kind of
    # observation that gets something deleted.
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

    @property
    def priority_at(self) -> datetime:
        """Queue position: how long the PERSON has been waiting, oldest first.

        DIFFERENT FROM `stalled_at`, and the difference is the whole point. For a
        capability ask, `stalled_at` is when the CAPABILITY shipped — which is right
        for staleness, because the thing worth reporting is that Grant can now do it.
        But using the same value to order the queue timestamps every reopened ask to
        "now", so they sort BEHIND every other subject.

        Measured on live data: declaring the four capabilities put Kerry, Jocelyn and
        Nelly at positions 14-18 of 19, roughly seven days of delivery behind the
        existing backlog — so the feature built to reach exactly those three people
        would not have reached any of them. Ordering by the date they actually ASKED
        puts a July question ahead of an August card, which is the honest priority.
        """
        asked = _parse(self.observed.get("asked_at_iso"))
        return asked or self.stalled_at


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
                    # Queue priority is how long the PERSON has waited, which is the
                    # ask date — not `available_since`, which is only the staleness
                    # clock. See NudgeCandidate.priority_at.
                    "asked_at_iso": str(row["asked_at"] or ""),
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

    `processing` IS INCLUDED, AND THAT IS THE IMPORTANT HALF. `claim_slack_event`
    writes that state before the work starts and `finish_slack_event` overwrites it
    after — so a process that DIES mid-turn leaves it there permanently. Observed
    live: a deploy restarted the listener 43 seconds into a question, and the thread
    still shows a "Thinking…" spinner that will never resolve. Every recovery path in
    the codebase looked only for `needs_reconciliation`, so that conversation was
    invisible to all of them — the precise shape of dead-end that lost reps in July.
    The grace period does the filtering: a turn takes seconds, so anything still
    `processing` a day later is dead, not busy.

    Only the LATEST receipt in a thread qualifies. If the person sent anything
    afterwards they came back on their own, and there is nothing to apologise for.
    """
    rows = conn.execute(
        """SELECT r.event_id,r.channel,r.thread_ts,r.slack_user,r.received_at,r.error
             FROM slack_event_receipts r
            WHERE r.state IN ('needs_reconciliation','processing')
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
    return sorted(ready, key=lambda item: item.priority_at)


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


def _target_local_hour(candidate: NudgeCandidate, now: datetime) -> int | None:
    """The hour where the mentioned rep actually is, or None when it is unknown.

    Unknown falls back to the shared window rather than blocking, so a rep with no
    recorded zone behaves exactly as before.
    """
    if not candidate.target_slack:
        return None
    zone = roster.timezone_for_slack(candidate.target_slack)
    if not zone:
        return None
    try:
        return now.astimezone(ZoneInfo(zone)).hour
    except (ValueError, KeyError):
        return None


def daily_slots(local_date: date, audience: str) -> tuple[time, ...]:
    """The Pacific target times a follow-up may land at today, earliest first.

    Seeded by (date, audience) so EVERY tick of a given day draws the SAME times — a
    per-tick roll would move the goalpost every 30 minutes, which is how the drip
    used to front-load its whole day into the first hour. Varies day to day, so two
    consecutive Tuesdays do not look identical.

    One slot per allowed daily nudge, and they are forced at least MIN_GAP apart so
    the randomness cannot accidentally stack both messages into the same half hour.
    """
    seed = random.Random(f"nudge:{local_date.isoformat()}:{audience}")
    start = NUDGE_BAND_START_PT.hour * 60 + NUDGE_BAND_START_PT.minute
    end = NUDGE_BAND_END_PT.hour * 60 + NUDGE_BAND_END_PT.minute
    gap = int(MIN_GAP.total_seconds() // 60)
    slots: list[time] = []
    earliest = start
    for index in range(MAX_NUDGES_PER_DAY):
        # Leave room for the remaining slots so the last one still fits in the band.
        remaining = MAX_NUDGES_PER_DAY - index - 1
        latest = end - remaining * gap
        if latest < earliest:
            break
        minutes = seed.randint(earliest, latest)
        slots.append(time(minutes // 60, minutes % 60))
        earliest = minutes + gap
    return tuple(slots)


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
    # A NUDGE @-MENTIONS ONE PERSON, so the coast-to-coast window is the wrong test
    # for it. `in_window` runs 7:00 Eastern to 17:00 Pacific — correct for a channel
    # card that pings nobody, but it permits 20:00 Eastern, and a targeted nudge at
    # 20:00 is a phone notification during someone's evening.
    #
    # Measured 2026-08-10: a send judged fine at 20:23 Pacific was 23:23 for Kerry,
    # who is in America/New_York. `--force` deliberately does NOT skip this one —
    # bypassing the shared window to run a test is defensible, waking a named person
    # at 11pm is not, and the two should not be behind the same switch.
    local = _target_local_hour(candidate, now)
    if local is not None and not 8 <= local < 18:
        return f"outside {candidate.target_slack}'s working hours"
    today = _sent_today(conn, candidate.audience, now)
    # HOLD FOR TODAY'S DRAWN MOMENT. Without this the worker fires on the first tick
    # it is allowed to, so a 30-minute cron delivers at the same minute every single
    # weekday and Grant reads as the cron job it is. The slots are drawn per day and
    # per channel, so the cadence looks human without becoming unpredictable to us.
    #
    # `force` skips it, like the business-hours window: both are about WHEN, and an
    # operator exercising the path should not have to wait out a random draw. The
    # recipient's own working hours above are NOT skippable, because that one is
    # about a person rather than a schedule.
    slots = daily_slots(now.astimezone(BUSINESS_TZ).date(), candidate.audience)
    if not force and len(today) < len(slots):
        due_at = slots[len(today)]
        if now.astimezone(BUSINESS_TZ).time() < due_at:
            return f"holding for today's {due_at:%H:%M} PT slot"
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


def run(
    client: WebClient | None,
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | None = None,
    audience: str = "",
) -> str:
    """Deliver at most ONE nudge per invocation, reserving before Slack is called.

    Ordering is guard → suppression → pacing → reserve → post, and the reservation is
    committed BEFORE the Slack call so a crash mid-send cannot produce a second nudge
    on the next tick. A dry run returns before any write.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # SCOPE A FORCED RUN TO ONE CHANNEL. `--force` skips the business-hours guard, so
    # without this the only way to exercise the path is to send into whichever
    # channel happens to be at the head of the queue — which is how a test becomes a
    # Sunday-evening notification to a colleague. Scoping makes the blast radius one
    # channel and one message, reviewable before it goes.
    #
    # Deliberately NOT a suppression: an out-of-scope subject is skipped without a
    # ledger row, so scoping a run can never retire a subject somewhere else.
    if not dry_run:
        # Close the loop on earlier sends BEFORE choosing this one's wording, so the
        # choice is made on the freshest evidence available.
        nudge_variants.mark_engagement(conn)
    for candidate in candidates(conn, current):
        if audience and candidate.audience != audience:
            continue
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
            # THREE outcomes, not two. Found by firing a real nudge at a thread that
            # did not exist: Slack rejected it outright, and the subject was recorded
            # `unknown` — "this may have landed, never retry" — which permanently
            # destroyed a follow-up that had definitively NOT been sent. One nudge per
            # subject ever means a burned subject is gone for good.
            #
            # A rejection Slack raises BEFORE accepting the message did not deliver
            # anything, and we can say so. Whether it is worth retrying depends on
            # whether the TARGET is broken or the moment was.
            if code in _BAD_TARGET:
                state = "suppressed"  # the anchor is wrong and will stay wrong
            elif code in _RETRYABLE:
                # Nothing was posted and the target is fine — release the
                # reservation so the next run can try again, rather than spending
                # the subject's single chance on a rate-limit.
                _release(conn, nudge_id, error=code)
                return f"nudge deferred ({code})"
            else:
                state = "unknown"  # may or may not have landed; never blind-retried
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


# Slack refused because the destination is wrong. Retrying cannot help.
_BAD_TARGET = frozenset(
    {
        "thread_not_found",
        "invalid_thread_ts",
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "msg_too_long",
        "invalid_auth",
        "account_inactive",
    }
)

# Slack refused for a reason that has nothing to do with this message. Nothing was
# posted, so the subject keeps its one chance.
_RETRYABLE = frozenset(
    {"ratelimited", "service_unavailable", "fatal_error", "request_timeout"}
)


def _release(conn: sqlite3.Connection, nudge_id: int, *, error: str) -> None:
    """Give a reserved subject its chance back after a definitively-failed send.

    Only ever called for errors where Slack rejected the request outright, so this
    can never produce a duplicate message: there is no message.
    """
    conn.execute(
        "DELETE FROM followup_nudges WHERE id=? AND state='reserved'", (nudge_id,)
    )
    conn.commit()


# How long a delivered offer is still plausibly what someone is answering. Kerry
# replied in three minutes; two days is generous, and keeps a stale hint from
# steering an unrelated conversation weeks later.
OFFER_STAYS_OPEN = timedelta(days=2)


def pending_capability_offer(
    conn: sqlite3.Connection, audience: str, thread_ts: str
) -> str:
    """The capability Grant OFFERED in this thread, if a follow-up is awaiting an answer.

    WHY THIS EXISTS — the first proactive follow-up Grant ever sent, and what happened
    three minutes later. It reached Kerry at 10:00 quoting her own July words, "Email
    those to kerry@monarchconnected.com… I can now — want me to send it?" She replied
    "Yes" at 10:03. Grant classified that as `draft_email` — PROSPECT outreach through
    Persequor — and answered "Tell me the exact Lead number you want to use." She had
    asked for her own spreadsheets and was handed a CRM question.

    The misread is understandable and that is exactly why prose cannot fix it: her
    quoted sentence CONTAINS an email address, so a model reading the thread sees a
    request to email somebody. A bare "Yes" carries no words of its own to correct it.

    So the offer is read from the ledger rather than inferred from the conversation.
    `followup_nudges` already records what was delivered, to which thread; this is that
    row, and it is the only honest source for "what did Grant just offer this person".
    """
    if not audience or not thread_ts:
        return ""
    # "I don't know what was offered" is a valid answer and must never be an
    # exception: this runs inside a live reply, and a lookup that cannot resolve
    # should cost the person a slightly worse answer, never their whole turn.
    # BOUNDED IN TIME. Nothing marks this row answered once the person replies, so
    # without a horizon the hint is prepended to EVERY turn in that thread forever —
    # and "yes, do that" about something else three weeks later would route back to
    # the capability Grant once offered. An offer nobody answered within two days has
    # lapsed, not stayed pending.
    cutoff = (datetime.now(timezone.utc) - OFFER_STAYS_OPEN).isoformat()
    try:
        row = conn.execute(
            """SELECT observed_json FROM followup_nudges
                WHERE audience=? AND anchor_ts=? AND state='delivered'
                  AND subject_kind='capability_now_available'
                  AND delivered_at>?
                ORDER BY delivered_at DESC LIMIT 1""",
            (audience, thread_ts, cutoff),
        ).fetchone()
    except Exception:  # noqa: BLE001 — see above; degrade, never raise
        return ""
    if row is None:
        return ""
    try:
        observed = json.loads(str(row["observed_json"] or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(observed.get("capability") or "")


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
