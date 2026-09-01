"""Proactive follow-ups: Grant chasing work that was started and left unfinished.

THE HONESTY BUDGET IS THE HARD PART. Silence in a Slack thread is not evidence that
nobody acted — the rep may have phoned the district from the car, or done the work in
Salesforce by hand. So a nudge never says "you didn't follow up". It says what Grant
observed IN ITS OWN RECORDS, and then asks. The difference between those two sentences
is the whole of rule 1 in a message that goes to a team channel.

WHERE A NUDGE LANDS DEPENDS ON WHO IT IS FOR. Most are THREADED REPLIES, which puts
them where the work already is, lets a reply re-enter a live Grant thread for free, and
needs neither a `posts` row nor a `proactive_daily_slots` claim — both carry CHECK
constraints that would reject a new kind. ESCALATIONS are the exception: they go to the
channel at top level, because their whole purpose is that somebody sees them (Chase,
2026-08-10). They carry a permalink instead of a thread.

ONE NUDGE PER SUBJECT, EVER. Enforced by the schema, not by this worker. Grant has no
evidence anyone read the first one, so a second is nagging; a rep who has deliberately
parked something should not be asked about it again every morning.

This module decides WHETHER, WHEN and TO WHOM. `nudge_sources.py` decides what is
outstanding, `nudge_messages.py` decides what it says, `nudge_promises.py` decides what
may be promised, and `nudge_silence.py` decides whether "nobody answered" is sayable.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .. import capability_asks, db, lead_claims, reminders, roster
from ..presentation import defuse_mentions
from ..migrations_nudges import NUDGE_STATES, NUDGE_SUBJECT_KINDS
from . import nudge_silence, nudge_variants
from .drip import PT as BUSINESS_TZ
from .drip import in_window
from .nudge_messages import build_message
from .nudge_sources import DROP_AFTER, GRACE, NudgeCandidate, candidates
from .nudge_sources import parse_ts as _parse

# Re-exported so `nudges.X` keeps working for every existing caller and test after the
# split. The names below are part of this module's public surface by long use.
__all__ = [
    "DROP_AFTER",
    "GRACE",
    "NUDGE_SUBJECT_KINDS",
    "NudgeCandidate",
    "build_message",
    "candidates",
    "run",
]

# Bumping this re-opens every subject for one more nudge under the new rules. It is
# part of the schema's uniqueness key precisely so that is a deliberate act.
POLICY_VERSION = "nudge-v1"

# A nudge is a phone notification for whoever is mentioned, so it is capped even
# though it does not consume the daily card slot.
MAX_NUDGES_PER_DAY = 2
MAX_NUDGES_PER_TARGET_PER_DAY = 1

# TWO HOURS, LOWERED FROM FOUR, AND THE PER-PERSON CAP IS WHY IT IS SAFE.
#
# `MIN_GAP` was doing two different jobs and failing the second. It spaces the two
# DRAWN slots — `daily_slots` forces them apart — and it also gates the actual send
# against the previous one. At four hours inside a six-hour band those two collide: the
# first slot can be drawn as late as 10:30, so a send delayed even slightly past its
# slot pushes the second requirement past 14:30 and out of the band entirely. The
# second delivery is then lost silently, reported as ordinary pacing, and the queue
# drains at half the intended rate — which matters against a backlog of ~30 subjects.
#
# It is safe to lower because `MAX_NUDGES_PER_TARGET_PER_DAY = 1` already guarantees
# the day's two nudges go to DIFFERENT people (or to nobody, for an untagged card). So
# this constant never protects one human from two notifications — that is the
# per-person cap's job. All it guards is channel noise, and two hours is ample for
# that: it still makes back-to-back posts impossible.
MIN_GAP = timedelta(hours=2)

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
# THE INSTALLED CRON IS `*/15 8-14 * * 1-5`, last tick 14:45 PT. Read off the droplet
# 2026-08-11 and checked empirically: over 1,432 drawn slots the latest is 14:30 and
# ZERO are unreachable, so the band clears the last tick by 15 minutes.
# CHANGE THEM TOGETHER.
#
# THIS COMMENT PREVIOUSLY CLAIMED `*/30 8-15 * * 1-5` AND WAS SIMPLY WRONG about
# production — safe, but not a description of the ground. Worse, CLAUDE.md recorded
# `15 9,14 * * 1-5`, whose last tick is 14:15: under that cron 252 of 1,432 slots
# (17.6%) are unreachable, and a day that draws a late first slot loses its second
# outright. Both were written from memory rather than from the crontab. If you are
# changing this band, go and read the crontab.
#
# The band ends at 14:30 rather than 15:00 for a SECOND reason, measured after the
# first version shipped: the recipient's own working-hours gate is `8 <= local < 18`,
# so for an Eastern rep 15:00 Pacific is 18:00 local and already refused. A slot
# drawn at the structural maximum would have been unreachable for Kerry — the same
# silent-hold class this band exists to avoid, arriving from the other end. 14:30 PT
# is 17:30 Eastern, which leaves margin for the tightest zone on the roster.
NUDGE_BAND_START_PT = time(8, 30)
NUDGE_BAND_END_PT = time(14, 30)

# Kinds posted to the CHANNEL at top level rather than threaded under the work.
#
# THIS REVERSES AN EARLIER DECISION, deliberately. Escalations used to be a DM to the
# manager, on the reasoning that reporting a colleague's silence does not belong where
# they can see it. Chase asked for the opposite (2026-08-10): "the system messages in
# the main Monarch Cloud Team channel and says something like, Hey @Anthony, @Jocelyn
# has not responded to me." His call, and the social cost is real — which is why every
# escalation wording says "nothing's come back here" and "may be handled offline", and
# never asserts that the person did nothing.
#
# A top-level post has no thread to reply into, so these carry a permalink instead.
CHANNEL_POST_KINDS = frozenset({"card_escalated", "offer_unanswered"})

# Kinds whose whole claim is "nobody answered". These MUST be verified against Slack
# itself before they are sent — see `nudge_silence`.
SILENCE_CLAIM_KINDS = frozenset({"card_escalated", "offer_unanswered"})

# Kinds that tell one person about another person's silence. An escalation may never
# be the FIRST thing that happens: the rep whose lead it is gets asked first.
ESCALATION_KINDS = frozenset({"card_escalated"})

# Pacing reasons that are facts about ONE CANDIDATE rather than about the day.
#
# THE DIFFERENCE DECIDES WHETHER THE QUEUE MOVES. `run` used to return on any pacing
# reason, so a single head-of-queue candidate whose recipient was asleep — or who had
# already had their one message — stopped every other subject behind it, on every tick,
# for as long as it stayed at the head. Reproduced by review: a card for a rep at 22:00
# their time blocked a fully sendable preview follow-up two places back, and reported
# the rep's clock as the reason nothing happened.
PERSON_ALREADY_NUDGED = "already nudged this person today"
_PERSON_HOURS_SUFFIX = "'s working hours"


def blocks_only_this_candidate(reason: str) -> bool:
    """Whether this pacing reason lets the NEXT subject be tried on the same tick."""
    return reason == PERSON_ALREADY_NUDGED or reason.endswith(_PERSON_HOURS_SUFFIX)


# The wordings a follow-up may use. Two, deliberately: enough to learn which reads
# better, few enough that every one is written and reviewed by a person rather than
# generated. See slack/nudge_variants.py for how one is chosen and measured.
VARIANTS = ("a", "b")

# Suppression reasons that are FACTS ABOUT THE SUBJECT and will never stop being
# true. Only these may be written to the ledger, because that write is permanent:
# the uniqueness key retires the subject under this policy version forever.
#
# `answered_since_offer` joins them because a person answering is exactly as permanent
# as the other four — once Jocelyn replies, there is nothing left to escalate about,
# and the subject should be retired rather than reconsidered every half hour.
#
# `lead_claimed` IS DELIBERATELY ABSENT, and the asymmetry with its neighbour
# `lead_parked` is the point rather than an oversight. Parking a lead is a triage
# state a human sets and Grant never revokes; a CLAIM is reversible by design, so
# writing it here would mean a rep saying "actually, it's not mine after all" leaves
# the claim undone and the follow-up permanently retired — the one-way door this
# feature was specifically built not to be (architectural-critic, 2026-09-01).
PERMANENT_SUPPRESSIONS = frozenset(
    {
        "stale",
        "resolved_since_queued",
        "engaged_since_queued",
        "lead_parked",
        "answered_since_offer",
    }
)


def suppress_reason(
    conn: sqlite3.Connection,
    candidate: NudgeCandidate,
    now: datetime,
    *,
    client: WebClient | None = None,
) -> str:
    """Why this nudge must NOT be sent, or '' when it may go.

    Re-checked immediately before the send, inside the reservation, so a subject that
    resolved while it sat in the queue produces silence rather than a false claim.

    `client` is optional so every existing caller and test keeps working, but a
    silence-claiming kind CANNOT PASS WITHOUT ONE: `_silence_reason` returns a
    suppression when it has no way to check. Fail closed is the whole design here.
    """
    if now > candidate.drop_after:
        return "stale"
    if db.channel_guard(conn, candidate.audience, now.isoformat()) is not None:
        return "channel_guard_active"
    if candidate.target_slack and reminders.is_opted_out(
        conn, candidate.target_slack, scope="nudges"
    ):
        return "opted_out"
    # AN OPT-OUT PROTECTS THE PERSON BEING TALKED ABOUT, NOT JUST THE ADDRESSEE.
    # `target_slack` on an escalation is the MANAGER, so the check above asks whether
    # the manager wants quiet — and would happily post "Jocelyn never answered" in a
    # public channel about somebody who had explicitly asked Grant to leave her alone.
    # Someone who opts out of follow-ups is opting out of being followed up ABOUT.
    subject_person = str(candidate.observed.get("silent_slack") or "") or str(
        candidate.observed.get("tagged_slack") or ""
    )
    if subject_person and reminders.is_opted_out(conn, subject_person, scope="nudges"):
        return "subject_opted_out"
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
        # A REP WHO SAID THEY WERE TAKING IT HAS ANSWERED, JUST NOT HERE. This is the
        # defect that prompted the whole feature: a claim made in another thread
        # leaves the card with no `engagement` row, so it still reads as ignored and
        # the manager is told nobody answered.
        #
        # READ LIVE, not from `observed`, unlike `lead_parked` above. A claim can land
        # between the candidate being built and the send, and this module's whole
        # posture is that anything which changed in between suppresses rather than
        # posting a claim that is no longer true.
        #
        # AND DELIBERATELY TRANSIENT — it is NOT in PERMANENT_SUPPRESSIONS, which is
        # the one thing about it that is easy to get wrong. `run()` writes a ledger
        # row only for a permanent reason, and that row's uniqueness key retires the
        # subject forever. A claim is the one suppression here that a human can
        # REVERSE, so recording it permanently would mean claim-then-release leaves
        # the claim undone and the follow-up destroyed, with nothing to show for it.
        # Held instead, the subject simply ages out at DROP_AFTER if the claim stands.
        if lead_claims.is_claimed(conn, int(candidate.observed.get("lead_id") or 0)):
            return "lead_claimed"
    if candidate.subject_kind in ESCALATION_KINDS:
        waiting = _escalation_is_premature(conn, candidate)
        if waiting:
            return waiting
    if candidate.subject_kind in CHANNEL_POST_KINDS:
        # THE ADDRESSEE HAS TO BE ABLE TO SEE IT. A mention notifies nobody who is not
        # in the conversation, so posting an escalation into a channel the manager has
        # not joined pays the whole social cost of naming a colleague publicly, buys
        # nothing, and reports success. Transient: an unknown answer suppresses without
        # recording, so an API hiccup cannot retire the subject.
        visible = nudge_silence.is_member(
            client, candidate.audience, candidate.target_slack
        )
        if visible is not True:
            return (
                "manager cannot see that channel"
                if visible is False
                else "could not verify channel membership"
            )
    if candidate.subject_kind in SILENCE_CLAIM_KINDS:
        return _silence_reason(client, candidate)
    return ""


def _escalation_is_premature(
    conn: sqlite3.Connection, candidate: NudgeCandidate
) -> str:
    """'' once the rep has had their own turn; a transient reason while they have not.

    THE ORDERING IS STRUCTURAL, NOT JUST A CONSTANT. `GRACE` already puts the rep's
    nudge at 24h and the manager's at 30h, but a constant is a hope: if the rep's
    nudge is held by the daily cap, or by their own working-hours gate, or by a Slack
    outage, the escalation can still come due and go out FIRST. A manager hearing that
    a colleague has not answered a message the colleague was never sent is the exact
    failure mode the grace period exists to prevent.

    An untagged card has no rep to wait for, so it escalates on its own timetable.
    Transient by design — this becomes false the moment the rep's nudge lands.

    ON THE ASYMMETRY, raised in review and deliberately kept: the rep's turn is a
    THREADED reply while the escalation naming them is a top-level channel post, which
    looks like the rep gets the quieter treatment. It is not a delivery-probability
    gap. `<@U…>` pushes a notification identically from a thread and from the channel,
    so the rep is notified either way; what differs is CHANNEL VISIBILITY, and that
    difference is the point of an escalation rather than a flaw in it. What would be
    unfair is the manager hearing FIRST, and that is what this function prevents.
    """
    if not str(candidate.observed.get("tagged_slack") or ""):
        return ""
    row = conn.execute(
        """SELECT state FROM followup_nudges
            WHERE subject_kind='card_unengaged' AND subject_id=? AND policy_version=?""",
        (candidate.subject_id, POLICY_VERSION),
    ).fetchone()
    # ANY row means the rep's turn has happened. A suppressed rep-nudge counts: the
    # reasons that suppress it permanently (parked, engaged, stale) also suppress this
    # escalation in the checks above, so anything still here was suppressed for a
    # reason that does not apply to the manager's copy.
    return "rep has not been asked yet" if row is None else ""


def _silence_reason(client: WebClient | None, candidate: NudgeCandidate) -> str:
    """'' only when Grant has POSITIVELY established that nobody replied.

    Both other answers suppress: a reply means there is nothing to escalate, and an
    unreadable thread means Grant cannot honestly say what happened in it. Neither is
    permanent — a reply is recorded by the caller as `answered_since_offer`, which is,
    and an unreadable thread is a transient failure that must not burn the subject.
    """
    channel = str(candidate.observed.get("channel") or candidate.audience)
    anchor = str(candidate.anchor_ts or "")
    # For an unanswered offer the clock starts at the OFFER, not the thread's start:
    # everything before it is the conversation the offer was made in.
    since = str(candidate.observed.get("offer_ts") or "") or anchor
    # THE TWO KINDS ASK DIFFERENT QUESTIONS, and using one for both was a real defect.
    # An unanswered offer claims "JOCELYN has not come back to me", so only Jocelyn can
    # answer it — reading any human reply as hers meant an uninvolved colleague asking
    # something unrelated in the thread permanently retired her follow-up, silently.
    # A card claims nobody picked it up, so anyone speaking settles it; there the
    # manager's own passing comment is the one that must not count.
    silent = str(candidate.observed.get("silent_slack") or "")
    replied = nudge_silence.replied_since(
        client,
        channel,
        anchor,
        since,
        only_user=silent,
        exclude_user="" if silent else candidate.target_slack,
    )
    if replied is True:
        return "answered_since_offer"
    if replied is None:
        return "could not verify silence"
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


def _day_start(now: datetime) -> str:
    """Midnight Pacific for the day `now` falls in, as a UTC ISO string."""
    local = now.astimezone(BUSINESS_TZ)
    start = datetime.combine(local.date(), time.min, BUSINESS_TZ)
    return start.astimezone(timezone.utc).isoformat()


def _sent_today(
    conn: sqlite3.Connection, audience: str, now: datetime
) -> list[sqlite3.Row]:
    """Nudges already reserved or delivered in this Pacific day for one channel."""
    return list(
        conn.execute(
            """SELECT target_slack,reserved_at FROM followup_nudges
                WHERE audience=? AND state IN ('reserved','delivered','unknown')
                  AND reserved_at>=?""",
            (audience, _day_start(now)),
        )
    )


def _sent_to_person_today(
    conn: sqlite3.Connection, target_slack: str, now: datetime
) -> int:
    """How many nudges this PERSON has had today, across every channel.

    THE PER-PERSON CAP IS ABOUT A PHONE, AND A PHONE DOES NOT KNOW WHICH CHANNEL A
    NOTIFICATION CAME FROM. Counting it per audience — as the daily cap correctly does
    — meant a rehearsal in the playground could double a colleague's real
    notifications for the day, and production plus playground plus a DM audience could
    put four messages out where the constants promise two. Reproduced by review.
    """
    if not target_slack:
        return 0
    row = conn.execute(
        """SELECT COUNT(*) FROM followup_nudges
            WHERE target_slack=? AND state IN ('reserved','delivered','unknown')
              AND reserved_at>=?""",
        (target_slack, _day_start(now)),
    ).fetchone()
    return int(row[0] or 0)


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
    # Counted across EVERY channel, not just this one — see `_sent_to_person_today`.
    if (
        candidate.target_slack
        and _sent_to_person_today(conn, candidate.target_slack, now)
        >= MAX_NUDGES_PER_TARGET_PER_DAY
    ):
        return PERSON_ALREADY_NUDGED
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
    plain_mentions: bool = False,
) -> str:
    """Deliver at most ONE nudge per invocation, reserving before Slack is called.

    Ordering is guard → suppression → pacing → reserve → post, and the reservation is
    committed BEFORE the Slack call so a crash mid-send cannot produce a second nudge
    on the next tick. A dry run returns before any write.

    `plain_mentions` renders `<@U…>` as a plain "@Name" so a rehearsal in the
    playground reads exactly like the real thing without sending anyone a phone
    notification. Chase asked for this explicitly while testing: "write at Anthony
    instead of actually tagging him so you do not ping him in Slack during testing."
    It changes ONLY the rendering — every guard, cap and ledger write is the live one,
    so what is being exercised is the real path.
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
        reason = suppress_reason(conn, candidate, current, client=client)
        if reason:
            # ONLY a permanent reason may be written. `_record` inserts the row whose
            # uniqueness key retires this subject forever, and several of the reasons
            # are transient: `channel_guard_active` is a Slack outage, `opted_out` can
            # be reversed, `could not verify silence` is an unreadable thread.
            # Recording those would mean one bad afternoon — or one rep asking for
            # quiet and later changing their mind — silently destroying every pending
            # follow-up in that channel, with nothing to show it had happened.
            # Measured on production, a single run during an outage would have burned
            # 22 subjects permanently.
            if not dry_run and reason in PERMANENT_SUPPRESSIONS:
                _record(conn, candidate, current, state="suppressed", reason=reason)
            continue
        waiting = pacing_reason(conn, candidate, current, force=force)
        # A DRY RUN REPORTS THE HOLD INSTEAD OF OBEYING IT. Pacing was evaluated before
        # the preview returned, so `nudge --dry-run` outside business hours — or once
        # the day's cap was spent — printed only "skip: outside business hours" and
        # showed nothing at all. That is the same false all-clear this command was
        # already fixed once for: an operator asking what Grant is about to say about a
        # colleague was shown an empty answer that looked like "nothing pending".
        if waiting and not dry_run:
            # A reason about ONE candidate must not stop the ones behind it.
            if blocks_only_this_candidate(waiting):
                continue
            return f"skip: {waiting}"
        variant = nudge_variants.choose(conn, candidate.subject_kind, VARIANTS)
        text = build_message(candidate, variant, conn=conn)
        if plain_mentions:
            text = _plainify_mentions(text)
        if dry_run:
            held = f" [held: {waiting}]" if waiting else ""
            return (
                f"[dry-run] would nudge {candidate.subject_kind} "
                f"({variant}){held}: {text}"
            )
        try:
            nudge_id = _record(
                conn, candidate, current, state="reserved", reason=None, variant=variant
            )
        except sqlite3.IntegrityError:
            # ANOTHER RUN GOT THERE FIRST. The cron carries no `flock`, and
            # `suppress_reason` now makes network calls, so two ticks can overlap for
            # seconds — long enough for both to pass the one-shot check above and race
            # to reserve the same subject. The UNIQUE key is what actually prevents the
            # duplicate message, and it does; before this the loser died with an
            # uncaught IntegrityError, so the job crashed rather than moving on. The
            # subject is taken, so skip to the next one.
            continue
        if client is None:
            return "skip: no Slack client configured"
        try:
            if candidate.subject_kind in CHANNEL_POST_KINDS:
                # A top-level post has no thread to reply into, so the work it is
                # about is linked instead. The permalink is fetched from Slack rather
                # than assembled from a guessed workspace URL — a broken link in an
                # escalation is worse than no link, because the whole message is "go
                # look at this".
                link = _permalink(
                    client,
                    str(candidate.observed.get("channel") or candidate.audience),
                    str(
                        candidate.observed.get("card_ts")
                        or candidate.observed.get("offer_ts")
                        or candidate.anchor_ts
                    ),
                )
                response = client.chat_postMessage(
                    channel=candidate.audience,
                    text=f"{text}\n{_labelled(link, candidate.subject_kind)}"
                    if link
                    else text,
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


def _plainify_mentions(text: str) -> str:
    """Strip every notifying form from a message so a rehearsal wakes nobody.

    "at Anthony", NOT "@Anthony" — Chase's exact instruction, and he is right to
    insist on the stricter form. A bare `@Anthony` in message text does not create a
    Slack mention and so does not push a notification, but that is a fact about the
    link syntax, not about the person: Slack also notifies on HIGHLIGHT WORDS, and a
    great many people have their own first name in that list. The whole point of a
    rehearsal is that no colleague's phone lights up, and "at" costs nothing.

    IT USED TO NEUTRALISE ONE FORM OUT OF SIX. `<@U…>` was handled; the legacy piped
    `<@U…|name>`, `<!here>`, `<!channel>`, `<!everyone>` and `<!subteam^S…>` all
    survived verbatim — so a rehearsal could notify an entire channel, which is a
    louder failure than the ping it was written to prevent. `defuse_mentions` now does
    the removing (one implementation, shared with the live path), and this only takes
    the remaining "@" off so nothing can reach a highlight word either.
    """
    names = {item.slack_id: item.name for item in roster.identities()}
    inert = defuse_mentions(text, names.get)
    # `defuse_mentions` leaves readable "@Name" / "@here"; a rehearsal drops the "@".
    return re.sub(r"@([A-Za-z][\w.\-]*)", lambda m: f"at {m.group(1)}", inert)


# What the link at the bottom of an escalation points AT, in the reader's terms. A
# raw permalink is 130 characters of query string under a one-line message, which
# looks like machine output in a channel full of people talking.
_LINK_LABEL = {
    "card_escalated": "See the card",
    "offer_unanswered": "See what I offered",
}


def _labelled(link: str, subject_kind: str) -> str:
    """One Slack link with words on it rather than a naked URL."""
    return f"<{link}|{_LINK_LABEL.get(subject_kind, 'Take a look')}>"


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


def _release(conn: sqlite3.Connection, nudge_id: str, *, error: str) -> None:
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
    if state not in NUDGE_STATES:
        raise ValueError(f"unknown nudge state {state!r}; add it to NUDGE_STATES")
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
    if state not in NUDGE_STATES:
        raise ValueError(f"unknown nudge state {state!r}; add it to NUDGE_STATES")
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

    IT ALSO REFUSES A CAPABILITY WITH NO SENTENCE, and that closes a real gap rather
    than duplicating the declare-time guard. `capability_asks.mark_available` rejects a
    slug with no hand-written wording, but it can only guard declarations made AFTER it
    shipped — a row armed earlier carries `available_since` already set and never passes
    through it again. This function is on the DELIVERY path, so it catches those too.
    Without it, such a row would render the generic "Good news — I can do that one now"
    to everyone who ever asked, which is unsendable-back.

    Production held 0 rows in that state when this was written; the point is that it
    stays 0 by construction instead of by luck.
    """
    if capability == "email_results":
        from ..notify import resend_client

        if not resend_client.is_configured():
            return False
    from .nudge_messages import wording_exists

    return wording_exists(capability)
