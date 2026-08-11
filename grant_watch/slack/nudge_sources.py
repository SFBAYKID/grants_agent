"""WHERE a follow-up comes from — every piece of unfinished work Grant can see.

Split out of `nudges.py` at the 1,000-line cap (CLAUDE.md rule 4), and the boundary is
a real one: this module answers "what is outstanding?", `nudges.py` answers "may it be
sent, when, and to whom?", and `nudge_messages.py` answers "what does it say?". They
change for different reasons — a source is edited when Grant gains a new way to observe
stalled work, the worker is edited when a guard was wrong, a sentence is edited when a
rep did not answer it.

EVERY CANDIDATE HERE IS AN OBSERVATION, NOT AN ACCUSATION. Grant sees Slack and its own
tables and nothing else. A row in this module means "Grant's records show nothing came
back", never "this person did nothing" — the rep may have phoned the district from the
car. The wording downstream is built to keep that distinction, and it is only keepable
because the candidate carries the evidence it was derived from in `observed`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import reminders, roster, territory
from ..migrations_nudges import NUDGE_SUBJECT_KINDS
from .drip import PT as BUSINESS_TZ

# How long after the work stalls Grant waits, and how long before it gives up. A
# nudge about something from three weeks ago is noise, not help.
GRACE = {
    "crm_preview_expired": timedelta(hours=1),
    "crm_batch_blocked": timedelta(days=1),
    "crm_batch_partial": timedelta(days=2),
    # ONE DAY, LOWERED FROM TWO (Chase, 2026-08-10). The card that prompted it —
    # $500,000 of verified SVPP money at North Palos District 117 — sat in the channel
    # with nobody answering it, and a lead whose spend window is already open does not
    # keep for two days before anyone is asked about it. One day still lets a rep who
    # saw the card at 4pm answer it the next morning on their own.
    "card_unengaged": timedelta(days=1),
    # No grace. The other kinds wait to see whether a human quietly finishes the work
    # anyway; there is nothing to wait for here, because the event being reported is
    # that Grant itself gained an ability.
    "capability_now_available": timedelta(0),
    # THIRTY HOURS, AND IT MUST STAY ABOVE `card_unengaged` (Chase asked for "24 to 30
    # hours"). The rep is asked at 24h and the manager hears at 30h, so the person
    # whose lead it is always gets the first word — escalating before anyone has been
    # asked is not a follow-up, it is telling on a colleague. `nudges.suppress_reason`
    # enforces the ordering structurally as well; this constant only makes the normal
    # case land in the right order.
    "card_escalated": timedelta(hours=30),
    # TWENTY-SIX HOURS. Chase's case: Grant told Jocelyn "I can build that campaign
    # now — want me to?" and she never answered, and nothing in the system ever
    # noticed. A working day plus a night is long enough that somebody who was simply
    # busy has had a fair chance, and short enough that the offer is still live.
    "offer_unanswered": timedelta(hours=26),
    # A day is long enough that someone who simply got pulled away has had a chance
    # to come back on their own, and short enough that the thread is still live.
    "thread_abandoned": timedelta(days=1),
}

# How long a subject stays worth mentioning. Five days made the eligible window only
# THREE days wide (grace takes the first two), which had a consequence nobody
# intended: every subject that accumulated while this feature was switched off aged
# past it before the feature could ever look at them. Measured on production the day
# it shipped, 28 of 36 due subjects were ALREADY unreachable. Fourteen days is still
# recent enough that a rep recognises what is being asked about, and wide enough that
# a one-a-day cap can actually drain a queue.
DROP_AFTER = timedelta(days=14)


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
        asked = parse_ts(self.observed.get("asked_at_iso"))
        return asked or self.stalled_at


def parse_ts(value: object) -> datetime | None:
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
        expires = parse_ts(row["expires_at"])
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
    # COUNT WHAT IS ACTUALLY STUCK, not the size of the batch. `blocked_resolution` is
    # set when ANY item is unresolved (`salesforce_campaign_batch.py:647`), so reporting
    # `unique_org_count` told a rep "still stuck on 14 orgs I can't match" about the
    # real California batch where 13 of 14 matched and exactly one was ambiguous. An
    # item is resolved iff `resolution_state='existing_record'`.
    rows = conn.execute(
        """SELECT b.id,b.channel,b.thread_ts,b.requested_by,b.state,b.updated_at,
                  b.unique_org_count,
                  (SELECT COUNT(*) FROM crm_campaign_batch_items i
                     JOIN crm_campaign_batch_targets t ON t.id=i.target_id
                    WHERE t.batch_id=b.id
                      AND i.resolution_state<>'existing_record') AS unresolved
             FROM crm_campaign_batches b
            WHERE b.state IN ('blocked_resolution','partial_by_user')"""
    ).fetchall()
    out: list[NudgeCandidate] = []
    for row in rows:
        stalled = parse_ts(row["updated_at"])
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
                observed=_batch_observed(row),
            )
        )
    return out


def _batch_observed(row: sqlite3.Row) -> dict[str, Any]:
    """Batch facts for the wording, with the stuck count only when it is known.

    A legacy batch with no item rows yields 0, which is not evidence that nothing is
    stuck — so `unresolved` is omitted entirely and the wording falls back to the
    total, exactly as it did before. Absent means unknown; it must not mean zero.
    """
    observed: dict[str, Any] = {"organizations": int(row["unique_org_count"] or 0)}
    unresolved = int(row["unresolved"] or 0)
    if unresolved:
        observed["unresolved"] = unresolved
    return observed


def _unengaged_cards(conn: sqlite3.Connection, now: datetime) -> list[NudgeCandidate]:
    """Cards that drew no reply, no reaction, and no CRM action.

    EVERY TIER, DELIBERATELY. There is no filter on `posts.kind` or `posts.style`, so
    a platinum award, a gold award, an RFP and a nugget are all followed up the same
    way. Chase asked for gold, silver and platinum by name; the honest implementation
    is to chase anything Grant posted a lead card for, because the tier describes how
    good the lead is, not whether silence about it matters.

    Grant knows only what happened in Slack and in its own tables. That is why the
    message says "nothing has come back HERE", never "nobody followed up".
    """
    rows = conn.execute(
        """SELECT p.id,p.channel,p.ts,p.posted_at,p.lead_id,p.kind,p.style,
                  l.entity_name,l.status,l.state,l.source,l.amount,l.lead_grade,
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
        posted = parse_ts(row["posted_at"])
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
        # AN OPT-OUT MEANS THE CARD NAMED NOBODY, so the follow-up must not name them
        # either. `drip.py` drops the routing mention when the owner has opted out —
        # the card still posts, because the lead belongs to the channel rather than to
        # one person — but this recomputed `tagged` from territory WITHOUT that filter.
        #
        # The result was a silent permanent stall, not a noisy one. `card_unengaged`
        # suppressed as `opted_out`, which is transient and writes no ledger row; and
        # `_escalation_is_premature` then waited forever for a `card_unengaged` row
        # that could never be written. Both subjects sat due and undeliverable until
        # they aged out, and nothing anywhere said so.
        #
        # Treating the card as untagged is the honest reading: nobody was asked, so the
        # follow-up asks the room and the escalation names no colleague.
        if tagged and reminders.is_opted_out(conn, tagged, scope="nudges"):
            tagged = ""
        observed = {
            "entity_name": str(row["entity_name"] or ""),
            "lead_status": str(row["status"] or ""),
            "lead_id": int(row["lead_id"] or 0),
            "amount_usd": int(row["amount"] or 0),
            "channel": str(row["channel"] or ""),
            "card_ts": str(row["ts"] or ""),
            "card_tier": _tier_of(row),
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
        # one-shot key, and its own opt-out check.
        #
        # IT NOW COVERS UNTAGGED CARDS TOO (Chase, 2026-08-10). The old rule required
        # a tagged rep, on the reasoning that "nobody replied" is not actionable. The
        # North Palos card disproved it: $500,000 of verified award money sat in the
        # channel unanswered, which is exactly when a manager wants to know. It is
        # actionable because the message now carries a concrete next step Grant has
        # verified it can actually perform — see `nudge_promises.best_offer`.
        #
        # The manager is never escalated to about their OWN card, and an escalation is
        # only ever posted to a real channel — `C…`. The offer path grew that guard in
        # d4c934d and this one did not; a card whose `posts.channel` was ever a DM
        # would otherwise put a message about a colleague into a private conversation
        # the manager is not in.
        if manager and manager != tagged and str(row["channel"] or "").startswith("C"):
            out.append(
                NudgeCandidate(
                    subject_kind="card_escalated",
                    subject_id=str(row["id"]),
                    # THE CHANNEL, NOT A DM (Chase, 2026-08-10): "the system messages
                    # in the main Monarch Cloud Team channel". This reverses the
                    # earlier design, which sent escalations privately so a rep would
                    # not see their own silence reported. Chase's call, and the cost
                    # is real and accepted — which is why the wording carries "may be
                    # handled offline" and never asserts the rep did nothing.
                    audience=str(row["channel"] or ""),
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
        shipped = parse_ts(row["available_since"])
        if shipped is None:
            continue
        asked_at = parse_ts(row["asked_at"])
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


def _unanswered_offers(conn: sqlite3.Connection) -> list[NudgeCandidate]:
    """Offers Grant made proactively that nobody ever answered.

    THE GAP THIS CLOSES, in Chase's words: "if Jocelyn does not respond, and let's say
    after 24 to 30 hours, the system messages in the main Monarch Cloud Team channel."

    Grant told Jocelyn at 14:15 "I can build that campaign now — want me to?", quoting
    her own 23 July words back to her. She did not reply, and NOTHING in the system
    was ever going to notice: `capability_now_available` closes the ask the moment it
    is delivered, and the one-shot key retires the subject forever. Delivery was being
    treated as completion. An offer nobody answers is not a finished piece of work; it
    is the most obviously unfinished one there is, because Grant opened it.

    THE SUBJECT IS THE DELIVERED NUDGE, not the ask. That matters for the one-shot
    key: `subject_id` is the `followup_nudges.id` of the offer, so one offer can
    produce at most one escalation, ever, even if the ask is later reopened under a
    new policy version.

    Silence is NOT established here. This function only proposes; whether the person
    actually stayed quiet is re-checked against Slack itself immediately before the
    send (`nudge_silence.replied_since`), because the local receipt table is known to
    undercount and the cost of getting this wrong is telling a manager that a
    colleague ignored a message she in fact answered.
    """
    rows = conn.execute(
        """SELECT id,audience,target_slack,anchor_ts,slack_ts,delivered_at,
                  observed_json,variant
             FROM followup_nudges
            WHERE subject_kind='capability_now_available'
              AND state='delivered'
              AND target_slack<>''
              AND slack_ts IS NOT NULL
              AND engaged_at IS NULL
            ORDER BY delivered_at DESC LIMIT 40"""
    ).fetchall()
    manager = roster.manager_slack_id()
    out: list[NudgeCandidate] = []
    for row in rows:
        delivered = parse_ts(row["delivered_at"])
        if delivered is None:
            continue
        silent = str(row["target_slack"] or "")
        # Nobody to tell, or the person who went quiet IS the manager — either way
        # there is no escalation to make. Fails closed, like `roster.manager_slack_id`
        # itself: no manager configured means no message rather than a guessed one.
        if not manager or manager == silent:
            continue
        # ONLY IN A CHANNEL. An escalation is delivered where the offer was made, and
        # production really does hold capability asks whose audience is a DM
        # (`D0BGW7EP3K5`). Posting there would be wrong twice: the manager is not in
        # that conversation, so a message addressed to them would be invisible and the
        # escalation would silently do nothing — and it would report the contents of
        # somebody's private conversation with Grant into that same private
        # conversation, which is not what "tell the manager" means. Slack ids:
        # `C…` is a channel, public or private; `D…` is a DM; `G…` a group DM.
        if not str(row["audience"] or "").startswith("C"):
            continue
        try:
            offered = json.loads(str(row["observed_json"] or "{}"))
        except json.JSONDecodeError:
            offered = {}
        out.append(
            NudgeCandidate(
                subject_kind="offer_unanswered",
                subject_id=str(row["id"]),
                audience=str(row["audience"] or ""),
                target_slack=manager,
                # The offer's own message, so the escalation threads under the thing
                # it is about and a reader can see the exact words in one click.
                anchor_ts=str(row["anchor_ts"] or ""),
                stalled_at=delivered,
                observed={
                    "silent_slack": silent,
                    "capability": str(offered.get("capability") or ""),
                    "ask_text": str(offered.get("ask_text") or ""),
                    "asked_on": str(offered.get("asked_on") or ""),
                    "offer_ts": str(row["slack_ts"] or ""),
                    "channel": str(row["audience"] or ""),
                    "delivered_at": str(row["delivered_at"] or ""),
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
        received = parse_ts(row["received_at"])
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
    """Every piece of unfinished work Grant can honestly ask about, fairly ordered.

    ONLY WORK THAT IS ALREADY DUE. The `due_at <= now` filter at the end means this
    returns nothing for a subject still inside its grace period — so an empty result
    means "nothing due", never "nothing exists". Reading it as absence is a mistake
    worth naming here, because it was made once already while measuring the queue.
    """
    found = (
        _abandoned_previews(conn)
        + _stalled_batches(conn)
        + _unengaged_cards(conn, now)
        + _capability_asks(conn)
        + _unanswered_offers(conn)
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
    return _fair_order(sorted(ready, key=lambda item: item.priority_at))


def _fair_order(by_age: list[NudgeCandidate]) -> list[NudgeCandidate]:
    """Round-robin across subject kinds, oldest-first WITHIN each kind.

    WHY STRICT AGE ORDER STARVES THE THING IT WAS BUILT FOR. `priority_at` sorts by
    how long the PERSON has waited, which is the honest priority for one capability
    ask against another — a July question should outrank an August one. But applied
    across ALL kinds it means every historical ask outranks every card, forever, and
    cards are the one kind that keeps arriving.

    Measured on production the day this shipped: 30 live subjects, and the North Palos
    card — $500,000 of verified SVPP money, the specific lead Chase raised because
    nobody engaged with it — sat at position 26. At two deliveries a day that is
    roughly thirteen days out, against a fourteen-day staleness horizon. It would very
    likely have aged out unmentioned, which is precisely the failure the card
    follow-up exists to prevent. A queue that never reaches a kind is not a queue with
    a long tail; it is a feature that does not run.

    So kinds take turns. The kind whose oldest member has waited longest goes first,
    then the next kind, and round again — so the July asks still lead. `priority_at`
    still decides who among Kerry, Nelly and Jocelyn is asked first.

    ROUND-ROBIN ALONE MADE IT WORSE, MEASURED. The first version of this function
    only interleaved kinds, and North Palos moved from 26th to **29th of 30** — the
    card it existed to rescue ended up last. Interleaving helps the OLDEST member of a
    SMALL kind (`offer_unanswered`, a kind of one, leapt 28 → 3). A freshly-posted
    card is the NEWEST member of the LARGEST kind, so it cannot help there, and
    putting the small kinds first pushed every card back.

    The deeper error was in the sort key, not the rotation. `priority_at` means "how
    long has the PERSON been waiting" — and A CARD HAS NO PERSON WAITING ON IT. Using
    `posted_at` there applies a people-fairness rule to a thing that is not a person,
    and buries every new arrival behind older, worse leads. So cards are ordered by
    the lead itself: tier first, then money, then freshness — the same grading
    CLAUDE.md already states ("Freshness is everything. An award from last month beats
    one from two years ago"). Every other kind keeps oldest-person-first, untouched.
    """
    queues: dict[str, list[NudgeCandidate]] = {}
    for item in by_age:
        queues.setdefault(item.subject_kind, []).append(item)
    # Kind order is set by each kind's OLDEST member — computed from the age-sorted
    # input BEFORE the re-sort below — so it cannot be gamed by a kind that simply has
    # more rows, and the overall head of the queue is unchanged.
    rotation = list(queues)
    for kind, queue in queues.items():
        if kind in _LEAD_KINDS:
            queue.sort(key=_lead_worth)
    out: list[NudgeCandidate] = []
    while any(queues[kind] for kind in rotation):
        for kind in rotation:
            if queues[kind]:
                out.append(queues[kind].pop(0))
    return out


# Subjects that are about a LEAD rather than about a person waiting for an answer.
_LEAD_KINDS = frozenset({"card_unengaged", "card_escalated"})

# How good a card is, best first. Anything unrecognised sorts LAST rather than being
# guessed at — which is safe, and was also how this quietly stopped working. See
# `_tier_of`.
_TIER_RANK = {"platinum": 0, "gold": 1, "rfp": 2, "silver": 2, "watch": 3, "nugget": 4}


def _tier_of(row: sqlite3.Row) -> str:
    """The card's grade, from the column that actually holds one.

    `posts.style` IS NOT A GRADE VOCABULARY, and assuming it was cost the ranking most
    of its effect. The first version read `style or kind`, which can only ever produce
    an unrecognised value when `style` is empty — `kind` holds `rich_award`,
    `award-brief`, `nugget`, none of which are tiers. Measured on production the day it
    shipped: **seven $500,000 awards ranked below a $364,891 gold card**, all seven
    scoring the unknown rank of 9. Locally `style` is worse still, holding free text
    like `worth-a-look` and `reconciled-live-thread`.

    `leads.lead_grade` is the real grading (gold/silver/watch). `style` is consulted
    first only because PLATINUM exists there and nowhere else — it is a presentation
    tier the drip assigns to an award fresh enough that a buy is imminent — and it is
    trusted only when it actually names a rank, so free-text styles fall through to
    the grade instead of poisoning it.
    """
    style = str(row["style"] or "").strip().lower()
    if style in _TIER_RANK:
        return style
    return str(row["lead_grade"] or "").strip().lower()


def _lead_worth(item: NudgeCandidate) -> tuple[int, int, float]:
    """Which unworked lead most deserves the next slot: tier, then money, then fresh.

    Negated numbers rather than `reverse=True`, because tier ascends while amount and
    recency descend and a single key has to carry all three.
    """
    tier = str(item.observed.get("card_tier") or "").lower()
    amount = int(item.observed.get("amount_usd") or 0)
    return (_TIER_RANK.get(tier, 9), -amount, -item.stalled_at.timestamp())
