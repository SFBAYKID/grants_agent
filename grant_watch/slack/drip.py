"""The drip engine: Grant surfaces one golden nugget at a time, sounding human.

Chase's spec: structured underneath (best lead_score first), sporadic on the surface
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

import math
import os
import random
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .. import db, scoring, territory
from ..presentation import display_entity_name, plain_fragment, state_display_name
from .search_presentation import record_link
from .source_status import _safe_url

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


def _fmt_amount(amount: float | None) -> str:
    """Format a finite positive source amount without silently dropping cents."""
    if amount is None or not math.isfinite(amount) or amount <= 0:
        return ""
    return f"${amount:,.2f}".removesuffix(".00")


def _award_facts(row: sqlite3.Row) -> tuple[str, str, str, str]:
    """Validate + extract the persisted award facts shared by nugget and platinum.

    Returns (entity, location, amount, program_text); raises on any unverified/missing
    fact so a proactive card is never built on incomplete evidence."""
    if str(row["current_event_verification_status"] or "") != "verified":
        raise ValueError("proactive award must be verified")
    if str(row["current_event_type"] or "") not in {
        "award_announced",
        "award_obligated",
    }:
        raise ValueError("proactive award has unsupported event type")
    entity = display_entity_name(row["entity_name"])
    if not entity:
        raise ValueError("proactive award requires an entity")
    # Nationwide polling means the code may be any state; an unrecognized code yields
    # no location rather than printing a bare abbreviation at a rep.
    state = plain_fragment(state_display_name(row["state"]))
    amt = _fmt_amount(row["amount"])
    if not amt:
        raise ValueError("proactive award requires a finite positive amount")
    location = f" in {state}" if state else ""
    program = plain_fragment(row["program"])
    program_text = f" {program}" if program else ""
    return entity, location, f" {amt}", program_text


def build_nugget(row: sqlite3.Row) -> tuple[str, str]:
    """Build one minimal award sentence using only persisted source facts."""
    entity, location, amount, program_text = _award_facts(row)
    return (
        f"{entity}{location} has a verified{amount}{program_text} funding award.",
        "award-brief",
    )


def build_platinum(row: sqlite3.Row) -> tuple[str, str]:
    """The cream: a security grant awarded in the last few days — the buyer is about to
    spend, so the card is timely and action-oriented (Chase: 'contact them now'). Facts
    only — same verified award data as a nugget, just worded for urgency."""
    entity, location, amount, program_text = _award_facts(row)
    return (
        f"{entity}{location} just landed a verified{amount}{program_text} security "
        "award and is about to spend it — worth reaching out now.",
        "platinum",
    )


def build_bulletin(row: sqlite3.Row) -> tuple[str, str]:
    """Build truthful program news from an official opportunity record.

    The opportunity title is the news, with the posting agency as fallback.
    """
    what = plain_fragment(row["title"] or row["entity_name"])
    if not what:
        raise ValueError("proactive bulletin requires a title or entity")
    close = f" through {row['funds_end'][:10]}" if row["funds_end"] else ""
    text = f"{what} is listed as open{close}."
    return text, "bulletin-open"


def _short_title(value: object, limit: int = 88) -> str:
    """Sanitized solicitation title shortened from the MIDDLE, keeping head and tail.

    Tail-cutting is wrong here, and it shipped that way once. Sibling solicitations
    from one project share a long prefix and differ only at the end — the two real PA
    Corrections rows are 'Sci Pine Grove - Control Room, Security Cameras and Other
    Facility Upgrades - **General and HVAC Construction**' and '… - **Plumbing
    Construction \\*REBID\\***'. Trimming the tail cut off the only words that told them
    apart, so both cards still read identically (caught by Chase in the 2026-07-22
    playground preview). Keeping both ends preserves what the RFP is AND which package.

    plain_fragment already strips URLs, punctuation and every Slack control character
    (<>@`*_~|), so nothing here can inject a mention or a link.
    """
    text = plain_fragment(value, max_length=240)
    if len(text) <= limit:
        return text
    head_budget = limit // 2
    head = text[:head_budget].rsplit(" ", 1)[0].rstrip(" ,;:-")
    tail_budget = limit - len(head)
    tail = text[-tail_budget:].split(" ", 1)[-1].lstrip(" ,;:-")
    return f"{head} … {tail}" if tail else f"{head}…"


_RFP_CAMERA_RE = re.compile(r"camera|surveillance|cctv|\bvideo\b", re.IGNORECASE)
_RFP_ACCESS_RE = re.compile(r"access control|door (?:access|hardening)|card reader",
                            re.IGNORECASE)


def build_rfp_alert(row: sqlite3.Row) -> tuple[str, str]:
    """One human sentence for an OPEN physical-security RFP a rep can act on now.

    Chase (2026-07-18): an open camera/access-control RFP is an active buyer — Grant
    should flag it individually ('… just opened an RFP … anybody want to talk?'). Kept
    honest: it says the RFP is OPEN with its verified deadline, never a posting date we
    did not read. The subject is drawn from the verified title/evidence, not invented.
    """
    if str(row["current_event_verification_status"] or "") != "verified":
        raise ValueError("proactive RFP must be verified")
    if str(row["current_event_type"] or "") != "rfp_posted":
        raise ValueError("proactive RFP has unsupported event type")
    entity = display_entity_name(row["entity_name"])
    if not entity:
        raise ValueError("proactive RFP requires an entity")
    haystack = (
        f"{row['title'] or ''} {row['current_event_evidence_excerpt'] or ''}"
    )
    camera = bool(_RFP_CAMERA_RE.search(haystack))
    access = bool(_RFP_ACCESS_RE.search(haystack))
    if camera and access:
        subject = "security cameras and access control"
    elif access:
        subject = "access control"
    elif camera:
        subject = "security cameras"
    else:
        subject = "physical security"
    due = str(row["funds_end"] or "")[:10]
    due_text = f", responses due {due}" if due else ""
    # Name the solicitation. Chase reported "the same card every morning" on
    # 2026-07-22; the leads were in fact DIFFERENT (verified in production: #9533
    # "…General and HVAC Construction" and #9565 "…Plumbing Construction *REBID*", two
    # trade packages of one SCI Pine Grove project). Because this sentence printed only
    # the agency, the regex-derived subject and the shared deadline, two genuinely
    # distinct RFPs rendered as identical text — indistinguishable from a repeat, and
    # useless to a rep who cannot tell which package they are being asked about.
    project = _short_title(row["title"])
    project_text = f" — {project}" if project else ""
    return (
        f"{entity} has an open RFP for {subject}{due_text}{project_text}. "
        "Anybody want to talk?",
        "rfp-open",
    )


def source_line(row: sqlite3.Row) -> str:
    """A separate, hyperlinked source line for a proactive alert (Chase 2026-07-19:
    hyperlink the label, don't show the raw URL, and leave a blank line before it).

    Every funding claim carries its source. The URL comes ONLY from the stored,
    per-record detail link and is hardened through _safe_url — a missing or unsafe URL
    yields no line rather than a bad one. Rendered as a Slack `<url|label>` link (the
    post uses mrkdwn); the URL never comes from untrusted text, and the label is fixed,
    so nothing injectable reaches the link."""
    try:
        url = record_link(row)
    except (KeyError, IndexError):
        return ""
    if not url:
        return ""
    safe = _safe_url(url)
    if safe == "(URL unavailable)":
        return ""
    return f"\n\n<{safe}|View the source record>"


def pacing_ok(
    conn: sqlite3.Connection,
    channel: str,
    now_utc: datetime,
    urgent: bool = False,
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
    """The full gate: window first, then pacing. Returns (go, reason)."""
    if force:
        return True, "forced"
    if not in_window(now_utc):
        return False, "outside Mon-Fri 7am ET – 5pm PT window"
    return pacing_ok(conn, channel, now_utc, urgent=urgent)


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


def _best_nugget(conn: sqlite3.Connection, nuggets: list[sqlite3.Row]) -> sqlite3.Row:
    """Top award by CRM-link tier then freshness-weighted score."""
    return max(
        nuggets,
        key=lambda r: (
            2
            if r["salesforce_opportunity_link"]
            else 1
            if r["salesforce_account_link"]
            else 0,
            scoring.lead_score(
                r["program"], r["amount"], r["current_event_occurred_on"] or ""
            )
            * scoring.feedback_multiplier(
                db.program_outcome_points(conn, r["program"] or "")
            ),
        ),
    )


def pick(
    conn: sqlite3.Connection, channel: str, today: date | None = None
) -> tuple[str, sqlite3.Row] | None:
    """Choose the single best opportunity of the day (Chase 2026-07-19). Quality ladder:
    PLATINUM (a security grant awarded in the last few days — a buy is imminent) first;
    then the top GOLD award; then a SILVER RFP (soonest deadline). Grants ALWAYS outrank
    RFPs — a district that already won money beats a solicitation, which is a lot of work
    with a low hit rate (so RFPs are silver at best, never surfaced above a grant). A
    program bulletin is the last resort. The daily cap keeps it to one."""
    today = today or datetime.now(timezone.utc).date()
    nuggets = db.nugget_candidates(conn, channel)
    platinum = [n for n in nuggets if _is_platinum(n, today)]
    if platinum:
        return "platinum", _best_nugget(conn, platinum)
    if nuggets:
        return "nugget", _best_nugget(conn, nuggets)
    rfps = db.rfp_candidates(conn, channel)  # open RFPs (silver), soonest deadline first
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


# Slack errors that describe the CHANNEL or the CREDENTIALS, not this particular card.
# Retrying the same lead is pointless and consuming it is destructive, so the lead is
# released and the failure is reported loudly instead.
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
        "ratelimited",
    }
)


def _ambiguous(
    conn: sqlite3.Connection, delivery_key: str, exc: BaseException
) -> str:
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
) -> str:
    """One cron tick: maybe post one thing. Returns a human-readable outcome."""
    now = datetime.now(timezone.utc)
    choice = pick(conn, channel, now.date())
    if choice is None:
        return "skip: nothing new worth saying"
    kind, row = choice
    # A platinum (or exceptional gold) award may take the rare emergency second slot.
    urgent = kind in ("platinum", "nugget") and _is_exceptional(row, now.date())
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
        text, style = builder(row)
    except ValueError as exc:
        # The renderers fail closed on unusable data (an entity that sanitizes to
        # nothing, a missing title) and they run BEFORE any reservation exists — so
        # nothing recorded the failure, the same top-ranked lead was re-picked on every
        # tick, and the tick crashed with a traceback that only cron.log ever saw. The
        # product went silent permanently while writing nothing anywhere. Quarantine the
        # lead durably so the candidate exclusion skips it AND `cli drip-blocked` can
        # show a human what was set aside.
        if not dry_run:
            db.quarantine_lead(
                conn,
                int(row["id"]),
                int(row["current_event_id"]) if row["current_event_id"] else None,
                channel,
                kind,
                str(exc),
            )
        return (
            f"skip: lead #{row['id']} cannot be rendered as a {kind} card ({exc}); "
            "quarantined and visible in `cli drip-blocked`"
        )
    # Hand the card to the rep who owns that state, then carry the source. Both lines
    # are separate blocks so the opening sentence still reads as one short human line.
    # The source is passed so a lead whose state was INFERRED from prose (the RFP
    # aggregator) can never tag a rep — see territory.VERIFIED_STATE_SOURCES.
    text = text + territory.mention_line(row["state"], row["source"]) + source_line(row)
    if dry_run:
        return f"[dry-run] would post {kind} ({style}): {text}"
    event_id = int(row["current_event_id"]) if row["current_event_id"] else None
    delivery_key = db.reserve_notification(
        conn,
        int(row["id"]),
        event_id,
        channel,
        kind,
        {"text": text, "style": style, "urgent": urgent},
    )
    if delivery_key is None:
        return "skip: this funding event is already reserved or delivered"
    assert client is not None
    try:
        resp = client.chat_postMessage(
            channel=channel,
            text=text,
            # mrkdwn on so the source renders as a hyperlink (Chase 2026-07-19). Safe:
            # the sentence is built only from sanitized facts (display_entity_name strips
            # <>*_~|@`), and the URL is the stored, hardened detail link — nothing
            # injectable reaches the render.
            mrkdwn=True,
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as exc:
        # Slack ANSWERED and refused. HTTP 200 with an `error` payload means the message
        # provably did not land, which is the opposite of ambiguous. Treating it as
        # ambiguous consumed a real lead per attempt: under a revoked token or a wrong
        # channel id that silently destroyed 1-2 gold leads every weekday while posting
        # nothing at all, because the reservation is what the caps and the candidate
        # exclusion both key off.
        if getattr(exc.response, "status_code", None) != 200:
            return _ambiguous(conn, delivery_key, exc)
        code = str(exc.response.get("error") or "unknown_error")
        if code in _SYSTEMIC_SLACK_ERRORS:
            # Nothing about THIS lead is wrong — the channel or the token is. Put the
            # lead straight back in the pool so a misconfiguration cannot eat inventory,
            # and say so loudly; every later tick will fail the same way until it is
            # fixed, which is the correct, visible behavior.
            db.release_notification(conn, delivery_key)
            return (
                f"halt: Slack rejected the post for this channel ({code}); no lead was "
                "consumed. Fix the channel or token — every tick will fail until then"
            )
        # Lead-specific refusal (e.g. msg_too_long): quarantine durably and visibly.
        db.finish_notification(conn, delivery_key, "rejected", error=code)
        return (
            f"skip: Slack rejected this card ({code}); lead #{row['id']} quarantined "
            "and visible in `cli drip-blocked`"
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
