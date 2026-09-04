"""The drip engine: Grant surfaces one golden nugget at a time, sounding human.

Chase's spec: structured underneath (newest award first, quality breaking ties —
"We should only be surfacing the most up to date cards", 2026-09-04), sporadic on the surface
(jittered timing, never a wall of leads). The initial message is one short factual sentence (RFP/platinum add a soft nudge),
with no links, buttons, or menu inline; the source link rides a separate line.

ONE best card a day (Chase 2026-07-18: more than that and people tune out); an
emergency may add a second. The single card is the best opportunity available, on a
quality ladder — it reads as varied without being random:
  platinum  a security grant awarded in the last few days — a buy is imminent (top)
  nugget    an entity that WON security money   ("Castle Rock SD has a $500K award")
  rfp       an entity with an OPEN security RFP ("… open RFP for security cameras …")
  bulletin  program-level news from grants.gov ("SVPP window just opened, closes 8/4")
Grants outrank RFPs — an RFP can be a formality with a vendor already chosen.

Each card is then addressed to the rep who owns that state (grant_watch/territory.py),
so it lands as a notification on one person's phone instead of as channel wallpaper.

Run via cron every ~30 min; each tick decides for itself whether to speak:
  in the window? (Mon-Fri, 7:00 America/New_York through 17:00 America/Los_Angeles)
  under the daily cap? past the min gap? and past TODAY'S SLOT — one target time
  drawn per day inside a configurable Pacific work-hours band, so the single card
  lands while the team is actually online instead of at 4 AM (see DEFAULT_SLOT_*).
Details and source links are available only after a human replies in the thread.
"""

from __future__ import annotations

import os
import random
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .. import db, scoring, territory, reminders
from .drip_card import render_blocks

# The card TEXT builders live in `drip_text.py` (split 2026-09-04 at the 1000-line
# cap); re-exported here because the tests, the CLI and the delivery path reach them
# as `drip.build_nugget` and friends. Pure functions over a row — no app state.
from .drip_text import (  # noqa: F401  (re-exported for the 25 `drip.*` call sites)
    _award_facts,
    _award_when,
    _event_date,
    _event_label,
    _fmt_amount,
    _short_title,
    build_bulletin,
    build_nugget,
    build_platinum,
    build_rfp_alert,
    source_line,
)

# Bulletin relevance: grants.gov phrase-search still lets through noise (live check
# 2026-07-13 surfaced 2011-era NSF programs). A bulletin must LOOK like our business.
# Bulletin relevance is precision-first (Chase: surface the RIGHT things). Bare
# words like "school", "safety", or "emergency" matched health-sector noise —
# live miss 2026-07-18: "Maternal Health Emergency Management Training" reached
# the channel. Require a strong physical-security phrase AND no off-domain term.
_BULLETIN_RELEVANT_RE = re.compile(
    r"school (?:security|safety|violence)|campus (?:security|safety)"
    r"|violence prevention|surveillance|access control|cctv|camera"
    r"|target hardening|hardening|physical security|security grant"
    r"|nonprofit security|svpp|cops (?:office|grant)|securing our schools",
    re.IGNORECASE,
)
_BULLETIN_OFFTOPIC_RE = re.compile(
    r"maternal|medical|clinical|disease|nursing|hospital|patient|opioid"
    r"|substance|behavioral health|mental health|medicaid|medicare",
    re.IGNORECASE,
)

# Chase (2026-07-18): ONE card a day is plenty — too many and people tune out. The
# single daily card is the best opportunity available (platinum > gold award > RFP),
# so it reads as varied without being random. Emergencies (urgent) may add ONE more.
DAILY_CAP = 1  # normal hard cap; only an urgent/emergency card exceeds it
ABSOLUTE_CAP = 2  # the daily card plus at most one emergency
MIN_GAP_MINUTES = 90  # never two posts closer than this
# Slack-refused deliveries do not spend the day's card — correct per delivery, since
# no human saw anything. In bulk it is a hazard: a renderer regression makes every
# card fail identically, and without a floor the drip would quarantine one gold lead
# every 30 minutes until the window closed. Three in a day is a broken renderer, not
# three unlucky leads.
MAX_REJECTIONS_PER_DAY = 3
BULLETIN_MAX_PER_DAY = 1
PLATINUM_DAYS = 7  # a security grant awarded within ~a week — the cream (buy imminent)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# The daily slot band, Pacific. Chase 2026-07-22: the old design rolled a flat 45%
# chance on every 30-minute tick starting at 4:00 AM PT, which front-loaded the single
# daily card so hard it was ~95% spent before 6 AM — verified in production, where the
# last three cards landed 04:30 / 04:00 / 05:00 PT to an empty office. Rolling per tick
# cannot be tuned into landing late; the fix is to choose ONE target time per day inside
# a work-hours band and post at the first tick after it. Still sporadic day to day
# (9:12, 8:34, 10:47…), but never before the team is at their desks.
# Env-tunable so the band can move without a deploy — Chase wants to try ~10:45 PT.
DEFAULT_SLOT_START_PT = "10:00"
DEFAULT_SLOT_END_PT = "11:30"


def in_window(now_utc: datetime) -> bool:
    """Mon-Fri, from 7:00 Eastern until 17:00 Pacific (Chase 2026-07-19) — the full
    coast-to-coast business day, opening on the East Coast and closing on the West."""
    et, pt = now_utc.astimezone(ET), now_utc.astimezone(PT)
    return et.weekday() < 5 and et.hour >= 7 and pt.hour < 17


def _parse_slot_time(raw: str, fallback: str) -> time:
    """Parse an 'HH:MM' band edge, falling back rather than crashing the cron tick.

    An UNSET variable is the normal case and is silent — warning on it would write two
    lines to cron.log on all 28 ticks a day and bury the outcomes that matter. Only a
    value someone actually typed, and typed wrong, is worth reporting.
    """
    configured = raw.strip()
    if configured:
        try:
            hour, _, minute = configured.partition(":")
            return time(int(hour), int(minute))
        except ValueError:
            print(
                f"[drip] ignoring malformed slot time {raw!r}; using {fallback}",
                file=sys.stderr,
            )
    hour, _, minute = fallback.partition(":")
    return time(int(hour), int(minute))


# The latest slot `in_window` can actually deliver. It closes at 17:00 PT and ticks run
# every 30 minutes, so a target after 16:30 has no tick left to fire on.
_LATEST_DELIVERABLE_PT = time(16, 30)
_EARLIEST_DELIVERABLE_PT = time(4, 0)  # 7:00 ET, when in_window opens


def slot_band() -> tuple[time, time]:
    """Return the configured Pacific band the daily card may land in.

    `DRIP_SLOT_START_PT` / `DRIP_SLOT_END_PT` ("HH:MM", Pacific) tune this without a
    deploy. Malformed values fall back and an inverted band collapses to a single slot.

    The band is also CLAMPED into the window `in_window` will actually admit. Without
    that, a plausible typo silences the product permanently and quietly: a band of
    17:00-17:30 draws a target `in_window` can never admit, so every tick logs
    `holding for today's 17:13 PT slot` and then `outside window` — two lines that both
    read as routine — and no card is ever posted again. This variable exists precisely
    so it can be retuned by hand, which is exactly when a typo happens.
    """
    start = _parse_slot_time(
        os.environ.get("DRIP_SLOT_START_PT", ""), DEFAULT_SLOT_START_PT
    )
    end = _parse_slot_time(os.environ.get("DRIP_SLOT_END_PT", ""), DEFAULT_SLOT_END_PT)
    clamped_start = min(max(start, _EARLIEST_DELIVERABLE_PT), _LATEST_DELIVERABLE_PT)
    clamped_end = min(max(end, clamped_start), _LATEST_DELIVERABLE_PT)
    if (clamped_start, clamped_end) != (start, end):
        print(
            f"[drip] slot band {start:%H:%M}-{end:%H:%M} PT is outside the deliverable "
            f"window; using {clamped_start:%H:%M}-{clamped_end:%H:%M} PT",
            file=sys.stderr,
        )
    return clamped_start, clamped_end


def daily_slot(local_date: date, channel: str) -> time:
    """The single Pacific target time today's card may post at or after.

    Seeded by (date, channel) so EVERY tick of a given day computes the SAME target —
    a per-tick roll would re-randomize the goalpost every 30 minutes and reintroduce
    exactly the front-loading this replaced. Varies day to day, so it still reads human.
    """
    start, end = slot_band()
    span = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    offset = random.Random(f"{local_date.isoformat()}:{channel}").randint(0, span)
    minutes = start.hour * 60 + start.minute + offset
    return time(minutes // 60, minutes % 60)


def pacing_ok(
    conn: sqlite3.Connection,
    channel: str,
    now_utc: datetime,
    urgent: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """Cap + gap + today's slot (window handled separately so each rule tests cleanly).

    Counts are taken from BOTH `posts` (written after the Slack call) and the delivery
    reservations in `notification_outbox` (written before it), and the larger of the two
    wins. Deriving the caps from `posts` alone made every one of them read zero whenever
    a confirmed send failed to record — and a zero count means no daily cap, no absolute
    cap, and no minimum gap, so the next tick posts again. See
    `db.delivery_attempts_today`. Reservations are the fail-closed signal: they cannot be
    missing for a message that reached Slack.
    """
    if db.rejections_today(conn, channel, now_utc) >= MAX_REJECTIONS_PER_DAY:
        # Stop consuming inventory. Repeated identical refusals mean the CARD is
        # broken, not the leads, and each one destroys a gold lead permanently.
        return False, (
            f"{MAX_REJECTIONS_PER_DAY} cards refused by Slack today — holding the "
            "rest of the queue until someone looks at why"
        )
    posts = db.posts_today(conn, channel, now_utc)
    attempts = db.delivery_attempts_today(conn, channel, now_utc)
    count = max(len(posts), len(attempts))
    if count >= ABSOLUTE_CAP:
        return False, f"absolute daily cap reached ({ABSOLUTE_CAP})"
    if count >= DAILY_CAP and not urgent:
        return False, f"daily cap reached ({DAILY_CAP})"
    if count >= DAILY_CAP and any(bool(post["urgent"]) for post in posts):
        return False, "daily cap reached; exceptional slot already used"
    # Gap is measured from the most recent evidence of EITHER kind, so an unrecorded
    # send still holds the line for MIN_GAP_MINUTES.
    stamps = [str(p["posted_at"]) for p in posts if p["posted_at"]]
    stamps += [str(a["created_at"]) for a in attempts if a["created_at"]]
    if stamps:
        last = datetime.fromisoformat(max(stamps))
        gap_min = (now_utc - last).total_seconds() / 60
        if gap_min < MIN_GAP_MINUTES:
            return (
                False,
                f"only {gap_min:.0f}m since last post (min {MIN_GAP_MINUTES}m)",
            )
    now_pt = now_utc.astimezone(PT)
    if force:
        # An operator override skips the day's TIMING, never its budget. The caps and
        # the gap above have already been applied.
        return True, "forced"
    if urgent:
        # An emergency may skip the day's random target, but NOT the start of the band.
        # Without this floor `urgent` reopened the 04:00 PT front-loading that the slot
        # design exists to prevent — a rep's phone at 4 AM is not a better outcome.
        opens = slot_band()[0]
        if now_pt.time() < opens:
            return False, f"urgent, but holding until the {opens:%H:%M} PT open"
        return True, "eligible"
    target = daily_slot(now_pt.date(), channel)
    if now_pt.time() < target:
        return False, f"holding for today's {target:%H:%M} PT slot"
    return True, "eligible"


def should_post(
    conn: sqlite3.Connection,
    channel: str,
    now_utc: datetime,
    force: bool = False,
    urgent: bool = False,
) -> tuple[bool, str]:
    """The full gate: window first, then pacing. Returns (go, reason).

    `force` is the operator override for "post now" — it skips the business-hours
    window and the day's randomized slot. It must NEVER skip the daily cap, and it
    used to: this function returned "forced" before pacing_ok was called at all, so
    the one command an operator reaches for during an incident was the only path
    that could post an unbounded number of cards in a day, each @-mentioning a rep.
    The budget is now applied to every path, and only the TIMING is overridable.
    """
    if not force and not in_window(now_utc):
        return False, "outside Mon-Fri 7am ET – 5pm PT window"
    return pacing_ok(conn, channel, now_utc, urgent=urgent, force=force)


def _is_exceptional(row: sqlite3.Row, today: date) -> bool:
    """Allow the rare fourth post only for a recent, verified, top-tier event."""
    occurred_raw = str(row["current_event_occurred_on"] or "")
    try:
        occurred = date.fromisoformat(occurred_raw[:10])
    except ValueError:
        return False
    if str(row["current_event_verification_status"] or "") != "verified":
        return False
    if occurred < today - timedelta(days=7) or occurred > today:
        return False
    base = scoring.lead_score(row["program"], row["amount"], occurred_raw, today)
    return base >= 0.85


def _is_platinum(row: sqlite3.Row, today: date) -> bool:
    """A verified PHYSICAL-security grant awarded within the last few days — the buyer
    just got the money and is about to spend, so it outranks everything (Chase)."""
    if str(row["current_event_verification_status"] or "") != "verified":
        return False
    occurred_raw = str(row["current_event_occurred_on"] or "")
    try:
        occurred = date.fromisoformat(occurred_raw[:10])
    except ValueError:
        return False
    if occurred < today - timedelta(days=PLATINUM_DAYS) or occurred > today:
        return False
    # only a physical-security program counts (SVPP/NSGP/CSSGP/PCCD), not any grant
    return scoring.PROGRAM_FIT.get(str(row["program"] or "").upper(), 0.0) >= 0.9


_STATE_COOLDOWN_CAP = 8  # never suppress more distinct states than this


def _nugget_sort_key(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> tuple[str, int, float]:
    """Ordering for a gold award: NEWEST FIRST, then CRM-link tier, then score.

    Chase, 2026-09-04: "We should only be surfacing the most up to date cards." Until
    then the date only entered through `lead_score`, which is flat for six months, so
    a $500,000 award five months old beat a $200,000 award three weeks old — and a
    Salesforce link beat both. The obligation date is now the first key: an ISO
    `YYYY-MM-DD` string orders correctly as text, and an undated row (which the
    ceiling already excludes) sorts last. Within one date — the shape every SVPP
    cohort has, hundreds of awards obligated on the same day — the old order holds:
    CRM link, then freshness-weighted score.

    This is the deterministic within/across-state order the diversity rule must
    preserve — diversity only chooses AMONG equally-eligible states, never reorders
    inside a state.
    """
    return (
        str(row["current_event_occurred_on"] or "")[:10],
        2
        if row["salesforce_opportunity_link"]
        else 1
        if row["salesforce_account_link"]
        else 0,
        scoring.lead_score(
            row["program"], row["amount"], row["current_event_occurred_on"] or ""
        )
        * scoring.feedback_multiplier(
            db.program_outcome_points(conn, row["program"] or "")
        ),
    )


def _best_nugget(
    conn: sqlite3.Connection,
    nuggets: list[sqlite3.Row],
    channel: str = "",
) -> sqlite3.Row:
    """Top award by quality, with STATE DIVERSITY so insertion order can't cluster.

    Chase's settled campaign direction (2026-07-22, [[grant-drip-campaign-direction]]):
    the honest gold cohort shares one program (SVPP) and one amount, and the pollers
    insert state-by-state, so a pure quality sort posts ~3 weeks of one state before the
    next. Verified in the offline preview against production: without this, the first
    nine cards were all Arizona.

    The rule is deterministic and preserves quality: prefer a state NOT among the last
    few posted (an adaptive cooldown, so rotation holds until barely any states remain),
    and among the preferred states pick the single highest-quality award. If every
    remaining candidate's state is in the cooldown (near the end of a run), the cooldown
    is dropped and the top-quality award wins outright — never a stall. Quality ordering
    WITHIN a state is untouched. `channel=""` disables the cooldown (used where there is
    no post history to rotate against), giving the original pure-quality behavior.
    """
    if channel:
        distinct_states = {str(n["state"] or "") for n in nuggets}
        cooldown = min(len(distinct_states) - 1, _STATE_COOLDOWN_CAP)
        if cooldown > 0:
            recent = db.recent_post_states(conn, channel, cooldown)
            preferred = [n for n in nuggets if str(n["state"] or "") not in recent]
            if (
                preferred
            ):  # fall back to the full set only if every state is on cooldown
                nuggets = preferred
    return max(nuggets, key=lambda r: _nugget_sort_key(conn, r))


def excluded_by_ceiling(conn: sqlite3.Connection, channel: str, today: date) -> int:
    """How many otherwise-postable gold awards the six-month ceiling is holding back.

    Diagnostic only — it changes no decision. It exists so the cron log can say
    "(162 gold awards on file past the six-month line)" instead of "nothing new",
    which after 2026-09-04 would otherwise be the whole story every tick.
    """
    return sum(
        1
        for n in db.nugget_candidates(conn, channel)
        if not scoring.award_is_card_fresh(n["current_event_occurred_on"], today)
    )


def pick(
    conn: sqlite3.Connection, channel: str, today: date | None = None
) -> tuple[str, sqlite3.Row] | None:
    """Choose the single best opportunity of the day (Chase 2026-07-19). Quality ladder:
    PLATINUM (a security grant awarded in the last few days — a buy is imminent) first;
    then the NEWEST GOLD award under the six-month ceiling (quality breaks ties within
    a date); then a SILVER RFP (soonest deadline). Grants ALWAYS outrank
    RFPs — a district that already won money beats a solicitation, which is a lot of work
    with a low hit rate (so RFPs are silver at best, never surfaced above a grant). A
    program bulletin is the last resort. The daily cap keeps it to one."""
    today = today or datetime.now(timezone.utc).date()
    # ONLY AN AWARD UNDER THE CEILING IS PUSHED. `nugget_candidates` is the grade's
    # view (gold = within a year); this card is the one that tags a person, and
    # `scoring.CARD_MAX_AWARD_MONTHS` is the rule for that. Filtered here rather than
    # in the query because this is where the tick's clock is, and because an empty
    # result must fall through to RFPs and bulletins exactly as an empty pool does —
    # never to an older award.
    nuggets = [
        n
        for n in db.nugget_candidates(conn, channel)
        if scoring.award_is_card_fresh(n["current_event_occurred_on"], today)
    ]
    platinum = [n for n in nuggets if _is_platinum(n, today)]
    if platinum:
        return "platinum", _best_nugget(conn, platinum, channel)
    if nuggets:
        return "nugget", _best_nugget(conn, nuggets, channel)
    rfps = db.rfp_candidates(
        conn, channel
    )  # open RFPs (silver), soonest deadline first
    silver_rfps = [r for r in rfps if str(r["lead_grade"]) == "silver"]
    if silver_rfps:
        return "rfp", silver_rfps[0]  # open RFP, soonest deadline
    bulletins_today = sum(
        1 for p in db.posts_today(conn, channel) if p["kind"] == "bulletin"
    )
    if bulletins_today < BULLETIN_MAX_PER_DAY:
        for cand in db.bulletin_candidates(conn, channel):
            title = cand["title"] or ""
            if _BULLETIN_RELEVANT_RE.search(title) and not _BULLETIN_OFFTOPIC_RE.search(
                title
            ):
                return "bulletin", cand
    return None


# Slack errors describing the CHANNEL or the CREDENTIALS, not this particular card.
# Retrying cannot help and consuming a lead per attempt is destructive, so the lead is
# released AND the channel is blocked until an operator clears it.
_SYSTEMIC_SLACK_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "no_permission",
        "org_login_required",
        "restricted_action",
    }
)

# Errors that are genuinely about THIS card's content — the only category permitted to
# quarantine a lead. Deliberately an allowlist: an unrecognized code must never cost a
# real lead, because "we do not know what went wrong" is not evidence that the lead is
# unusable (Chase, 2026-07-22). Anything unlisted releases the lead instead.
_CONTENT_SLACK_ERRORS = frozenset(
    {
        "msg_too_long",
        "invalid_blocks",
        "invalid_block_part",
        "blocks_too_long",
        "invalid_attachments",
        "no_text",
    }
)


_DEFAULT_RETRY_AFTER = 60  # Slack's documented minimum when the header is absent

# A systemic Slack failure blocks the channel for a BOUNDED period, not forever. A
# permanent block would simply trade one silent wedge for another: the only alarm this
# system has is a non-zero exit and a line in cron.log, and the droplet crontab has no
# MAILTO and redirects everything with `>>`, so nothing reads either. Bounded means the
# product self-heals the moment Slack does. Escalating means a genuine outage is not
# hammered every 30 minutes: 1h, 2h, 4h, then 8h.
_SYSTEMIC_BACKOFF_BASE_MINUTES = 60
_SYSTEMIC_BACKOFF_MAX_MINUTES = 480


def _systemic_backoff_minutes(attempts: int) -> int:
    """Escalating, capped backoff for consecutive systemic failures."""
    exponent = max(0, min(attempts - 1, 8))
    return min(
        _SYSTEMIC_BACKOFF_BASE_MINUTES * (2**exponent), _SYSTEMIC_BACKOFF_MAX_MINUTES
    )


def _log_lead_quarantine(channel: str, lead_id: int, kind: str, reason: str) -> None:
    """One structured line whenever a real lead is permanently set aside.

    A quarantine destroys inventory just as surely as a channel block stops it, and it
    used to report as a routine `skip:` with exit 0 — indistinguishable from a quiet
    day. Same treatment as a block: structured line, and a non-zero exit via
    `cli.FAILING_DRIP_OUTCOMES`.
    """
    print(
        f"[drip][CRITICAL] lead_quarantined audience={channel} lead_id={lead_id} "
        f"kind={kind} reason={reason}",
        file=sys.stderr,
    )


def _incident_lapsed(prior: sqlite3.Row, now: datetime) -> bool:
    """Whether a prior guard is stale enough to count as a FINISHED incident.

    A guard is only cleared by a successful post, so a quiet stretch (empty pool, cap
    spent, weekend) can leave an expired row behind indefinitely. Treat anything whose
    hold ended longer ago than the maximum backoff as a closed incident, so the next
    failure escalates from the beginning rather than inheriting a months-old count.

    Measured from the guard's EXPIRY, not its last write. Measuring from `updated_at`
    made every incident lapse at each 8-hour boundary — because `available_at` IS
    `updated_at + 8h` at the cap — so the ladder ran 1h→8h→1h→8h and never held.
    """
    try:
        expired_at = datetime.fromisoformat(str(prior["available_at"]))
    except (TypeError, ValueError):
        return True
    return (now - expired_at) > timedelta(minutes=_SYSTEMIC_BACKOFF_MAX_MINUTES)


def _log_channel_block(
    channel: str, code: str, until: str, attempts: int, first_failure: str
) -> None:
    """Emit ONE structured critical line per block period, to stderr (cron.log).

    Deliberately not a Slack message: the thing that failed IS Slack, so reporting a
    Slack outage through the same credential and channel is not a report. Deliberately
    not MAILTO either — no working mail transport has been proven on that box. This is
    an honest local record; a genuinely independent external alert is separate work and
    is NOT claimed here.
    """
    print(
        f"[drip][CRITICAL] channel_blocked audience={channel} error={code} "
        f"blocked_until={until} consecutive_periods={attempts} "
        f"first_failure={first_failure}",
        file=sys.stderr,
    )


def _retry_after_seconds(exc: SlackApiError) -> int:
    """Read Slack's Retry-After header, clamped to something a 30-min cron can honour.

    Missing or unparseable headers fall back to a conservative default rather than
    retrying immediately — the point of a backoff is to stop hammering the API.
    """
    headers = getattr(exc.response, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after") or ""
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER
    return max(1, min(seconds, 3600))


def _ambiguous(conn: sqlite3.Connection, delivery_key: str, exc: BaseException) -> str:
    """Record a delivery whose outcome genuinely cannot be determined.

    A timeout or a 5xx may mean Slack accepted the post, so the reservation is KEPT —
    that is what prevents a duplicate — and the lead is permanently set aside rather
    than retried. `cli drip-blocked` lists these so the loss is visible, not silent.
    """
    db.finish_notification(conn, delivery_key, "unknown", error=type(exc).__name__)
    return (
        "unknown: Slack delivery could not be confirmed; Grant will not "
        "auto-retry this event to avoid a duplicate (see `cli drip-blocked`)"
    )


def run_drip(
    client: WebClient | None,
    channel: str,
    conn: sqlite3.Connection,
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> str:
    """One cron tick: maybe post one thing. Returns a human-readable outcome.

    `now` is injectable so a test can express "the next day" rather than relying on
    an override to skip the daily cap — matching salesforce_followups.run and
    nudges.run, which both already take a clock.
    """
    now = now or datetime.now(timezone.utc)
    # THE UTC DATE, like `daily_list.run`, `delivery.run` and the nudge gate, so one
    # award is inside or outside the six-month line on every surface at once.
    today = now.astimezone(timezone.utc).date()
    # A channel-level guard stops the tick before anything else. `blocked` means Slack
    # told us the channel or token is wrong and only an operator can clear it; without
    # this, every 30-minute tick failed identically and (before the release fix) ate a
    # lead each time. `backoff` self-clears once Slack's Retry-After has elapsed.
    guard = db.channel_guard(conn, channel)
    if guard is not None and not force:
        state, reason = str(guard["state"]), str(guard["last_error"] or "")
        if state == "blocked":
            return (
                f"blocked: this channel is blocked ({reason}); no post attempted. "
                "Clear it with `cli drip-unblock` once Slack is fixed"
            )
        return f"backoff: holding until {guard['available_at']} ({reason})"
    choice = pick(conn, channel, today)
    if choice is None:
        held = excluded_by_ceiling(conn, channel, today)
        # NAME THE HELD-BACK COUNT IN THE ONE LINE AN OPERATOR READS. After the
        # ceiling shipped the whole 2025-10-10 cohort became unpostable at once, and
        # "nothing new" on its own is indistinguishable from an empty pool.
        return (
            f"skip: nothing new worth saying ({held} gold award"
            f"{'' if held == 1 else 's'} on file past the six-month line)"
            if held
            else "skip: nothing new worth saying"
        )
    kind, row = choice
    # A platinum (or exceptional gold) award may take the rare emergency second slot.
    urgent = kind in ("platinum", "nugget") and _is_exceptional(row, today)
    go, reason = should_post(conn, channel, now, force=force, urgent=urgent)
    if not go:
        return f"skip: {reason}"
    builder = {
        "platinum": build_platinum,
        "nugget": build_nugget,
        "rfp": build_rfp_alert,
        "bulletin": build_bulletin,
    }[kind]
    try:
        # The award builders take the tick's own clock so the age they print and the
        # pacing decisions above cannot disagree about what day it is.
        text, style = (
            builder(row, today) if kind in ("platinum", "nugget") else builder(row)
        )
    except ValueError as exc:
        # The renderers fail closed on unusable data (an entity that sanitizes to
        # nothing, a missing title) and they run BEFORE any reservation exists — so
        # nothing recorded the failure, the same top-ranked lead was re-picked on every
        # tick, and the tick crashed with a traceback that only cron.log ever saw. The
        # product went silent permanently while writing nothing anywhere. Quarantine the
        # lead durably so the candidate exclusion skips it AND `cli drip-blocked` can
        # show a human what was set aside.
        if dry_run:
            return (
                f"[dry-run] lead #{row['id']} cannot be rendered as a {kind} card "
                f"({exc}); WOULD quarantine it (nothing was written)"
            )
        db.quarantine_lead(
            conn,
            int(row["id"]),
            int(row["current_event_id"]) if row["current_event_id"] else None,
            channel,
            kind,
            str(exc),
        )
        _log_lead_quarantine(channel, int(row["id"]), kind, f"unrenderable: {exc}")
        return (
            f"quarantined: lead #{row['id']} cannot be rendered as a {kind} card "
            f"({exc}); set aside and visible in `cli drip-blocked`"
        )
    # Hand the card to the rep who owns that state, then carry the source. Both lines
    # are separate blocks so the opening sentence still reads as one short human line.
    # The source is passed so a lead whose state was INFERRED from prose (the RFP
    # aggregator) can never tag a rep — see territory.VERIFIED_STATE_SOURCES.
    sentence = text
    routing = territory.routing_line(row["state"], row["source"])
    # AN OPT-OUT HAS TO REACH THE LOUDEST SENDER TOO. The routing line is a literal
    # @-mention, which is a phone notification — exactly the thing someone means when
    # they say "stop pinging me". `stop_followups` promises to switch off ALL of
    # Grant's proactive messages, and until now the daily card ignored it entirely,
    # which made that promise false. The CARD still posts: the lead is the channel's,
    # not one person's. Only the mention is dropped.
    owner = territory.owner_for_state(row["state"])
    if routing and owner and reminders.is_opted_out(conn, owner, scope="nudges"):
        routing = ""
    source = source_line(row)
    text = sentence + routing + source
    # The same three strings, restyled into the rich-campaign Block Kit layout (Chase
    # 2026-08-05). `text` stays the complete message for notifications/screen readers.
    blocks = render_blocks(kind, sentence, routing, source)
    if dry_run:
        return f"[dry-run] would post {kind} ({style}): {text}"
    event_id = int(row["current_event_id"]) if row["current_event_id"] else None
    delivery_key = db.reserve_notification(
        conn,
        int(row["id"]),
        event_id,
        channel,
        kind,
        {"text": text, "style": style, "urgent": urgent, "blocks": blocks},
    )
    if delivery_key is None:
        return "skip: this funding event is already reserved or delivered"
    assert client is not None
    try:
        resp = client.chat_postMessage(
            channel=channel,
            text=text,
            # The blocks carry the SAME sanitized strings in the rich layout; `text`
            # remains the complete fallback. mrkdwn on so the source renders as a
            # hyperlink (Chase 2026-07-19). Safe: the sentence is built only from
            # sanitized facts (display_entity_name strips <>*_~|@`), and the URL is
            # the stored, hardened detail link — nothing injectable reaches the render.
            blocks=blocks,
            mrkdwn=True,
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as exc:
        status = getattr(exc.response, "status_code", None)
        # RATE LIMITED. Not ambiguous and not this lead's fault: Slack is telling us to
        # wait. Release the lead and persist a backoff so later ticks honour Retry-After
        # instead of hammering the API and burning inventory one card per tick.
        if status == 429:
            db.release_notification(conn, delivery_key)
            retry_after = _retry_after_seconds(exc)
            until = (now + timedelta(seconds=retry_after)).isoformat(timespec="seconds")
            db.set_channel_guard(
                conn,
                channel,
                "backoff",
                f"ratelimited; retry after {retry_after}s",
                available_at=until,
            )
            return (
                f"backoff: Slack rate-limited this channel; holding {retry_after}s "
                "(no lead consumed)"
            )
        # Slack ANSWERED and refused. HTTP 200 with an `error` payload means the message
        # provably did not land — the opposite of ambiguous. Treating it as ambiguous
        # consumed a real lead per attempt.
        if status != 200:
            return _ambiguous(conn, delivery_key, exc)
        code = str(exc.response.get("error") or "unknown_error")
        if code in _SYSTEMIC_SLACK_ERRORS:
            # The channel or the token is wrong, not this lead. Release the lead, then
            # block the channel for a BOUNDED, escalating period so later ticks neither
            # hammer Slack nor go silent forever. After it expires exactly one attempt
            # is made; a success clears the guard, a repeat renews it — and no lead is
            # consumed either way.
            db.release_notification(conn, delivery_key)
            prior = db.channel_guard_any(conn, channel, "blocked")
            now_iso = now.isoformat(timespec="seconds")
            # A NEW incident starts fresh. Without this, a guard left behind by an
            # outage months ago (never cleared, because clearing needs a successful
            # post and the pool may simply have been empty) made an unrelated first
            # failure inherit its count and jump straight to an 8-hour block.
            continuing = prior is not None and not _incident_lapsed(prior, now)
            attempts = (int(prior["attempts"]) + 1) if continuing else 1
            minutes = _systemic_backoff_minutes(attempts)
            until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            # The FIRST line of an incident previously reported first_failure as the
            # future unblock time — the one line an operator greps for was the wrong one.
            first_failure = str(prior["created_at"]) if continuing else now_iso
            db.set_channel_guard(
                conn,
                channel,
                "blocked",
                code,
                available_at=until,
                reset=not continuing,
            )
            _log_channel_block(channel, code, until, attempts, first_failure)
            return (
                f"blocked: Slack rejected posting to this channel ({code}); no lead was "
                f"consumed. Holding {minutes}m until {until}; `cli drip-unblock` "
                "resumes sooner once Slack is fixed"
            )
        if code in _CONTENT_SLACK_ERRORS:
            db.finish_notification(conn, delivery_key, "rejected", error=code)
            _log_lead_quarantine(channel, int(row["id"]), kind, f"rejected: {code}")
            return (
                f"quarantined: Slack rejected this card ({code}); lead #{row['id']} "
                "set aside and visible in `cli drip-blocked`"
            )
        # UNRECOGNIZED code. We do not know what went wrong, and not knowing is not
        # evidence that the lead is unusable — so it must NOT be quarantined. Release it
        # and report loudly; a real lead is worth more than a tidy error path.
        db.release_notification(conn, delivery_key)
        return (
            f"error: Slack refused this post with an unrecognized code ({code}); "
            f"lead #{row['id']} was released, not quarantined. Needs a human"
        )
    except Exception as exc:  # noqa: BLE001 — timeout is ambiguous; never blind-retry
        return _ambiguous(conn, delivery_key, exc)
    # Post-send bookkeeping: the message is ALREADY in Slack, so a failure here must not
    # crash the cron tick or leave the outbox stuck in 'sending' (an orphaned reservation
    # silently wedges the picker's ladder — the stuck lead stays top-ranked and blocks
    # every tier beneath it). Finalize best-effort and report honestly instead.
    try:
        db.record_post(
            conn,
            kind,
            int(row["id"]),
            channel,
            resp["ts"],
            style,
            delivery_key=delivery_key,
            event_id=event_id,
            urgent=urgent,
        )
        db.finish_notification(conn, delivery_key, "delivered", slack_ts=resp["ts"])
        db.mark_surfaced(conn, [int(row["id"])])
        # A confirmed delivery proves the channel and token work again, so retire any
        # guard. This is the normal WRITABLE path — reads never clear a guard.
        db.clear_channel_guard(conn, channel, ("blocked", "backoff"))
    except Exception as exc:  # noqa: BLE001 — never crash the tick after a confirmed send
        try:
            db.finish_notification(conn, delivery_key, "delivered", slack_ts=resp["ts"])
            db.mark_surfaced(conn, [int(row["id"])])
        except Exception:  # noqa: BLE001 — last-ditch; the message still went out
            pass
        return (
            f"posted {kind} ({style}) for lead #{row['id']}, but recording it hit "
            f"{type(exc).__name__}; the message is in Slack and will not be repeated"
        )
    return f"posted {kind} ({style}) for lead #{row['id']}: {row['entity_name']}"
