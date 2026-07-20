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

Run via cron every ~30 min; each tick decides for itself whether to speak:
  in the window? (Mon-Fri, 8:00 America/New_York through 17:00 America/Los_Angeles)
  under the daily cap? past the min gap? and a random skip so timing feels human.
Details and source links are available only after a human replies in the thread.
"""

from __future__ import annotations

import random
import re
import sqlite3
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

from .. import db, scoring
from ..presentation import display_entity_name, plain_fragment
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
DAILY_AIM = 1  # normal target: one best-of-the-day card
DAILY_CAP = 1  # normal hard cap; only an urgent/emergency card exceeds it
ABSOLUTE_CAP = 2  # the daily card plus at most one emergency
MIN_GAP_MINUTES = 90  # never two posts closer than this
POST_PROBABILITY = 0.45  # per-eligible-tick chance — the "sporadic" in the spec
BULLETIN_MAX_PER_DAY = 1
PLATINUM_DAYS = 7  # a security grant awarded within ~a week — the cream (buy imminent)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

_STATE_NAMES = {
    "WA": "Washington",
    "CA": "California",
    "MI": "Michigan",
    "PA": "Pennsylvania",
    "OR": "Oregon",
}


def in_window(now_utc: datetime) -> bool:
    """Mon-Fri, from 8:00 Eastern until 17:00 Pacific (per Chase)."""
    et, pt = now_utc.astimezone(ET), now_utc.astimezone(PT)
    return et.weekday() < 5 and et.hour >= 8 and pt.hour < 17


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
    state_code = plain_fragment(row["state"]).upper()
    state = plain_fragment(_STATE_NAMES.get(state_code, state_code))
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
    return (
        f"{entity} has an open RFP for {subject}{due_text}. Anybody want to talk?",
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
    rng: random.Random,
    urgent: bool = False,
) -> tuple[bool, str]:
    """Cap + gap + jitter (window handled separately so each rule tests cleanly)."""
    today = db.posts_today(conn, channel, now_utc)
    if len(today) >= ABSOLUTE_CAP:
        return False, f"absolute daily cap reached ({ABSOLUTE_CAP})"
    if len(today) >= DAILY_CAP and not urgent:
        return False, f"daily cap reached ({DAILY_CAP})"
    if len(today) >= DAILY_CAP and any(bool(post["urgent"]) for post in today):
        return False, "daily cap reached; exceptional slot already used"
    if today:
        last = datetime.fromisoformat(today[-1]["posted_at"])
        gap_min = (now_utc - last).total_seconds() / 60
        if gap_min < MIN_GAP_MINUTES:
            return (
                False,
                f"only {gap_min:.0f}m since last post (min {MIN_GAP_MINUTES}m)",
            )
    probability = POST_PROBABILITY if len(today) < DAILY_AIM else 0.25
    if not urgent and rng.random() > probability:
        return False, "jitter skip (keeps timing feeling human)"
    return True, "eligible"


def should_post(
    conn: sqlite3.Connection,
    channel: str,
    now_utc: datetime,
    rng: random.Random,
    force: bool = False,
    urgent: bool = False,
) -> tuple[bool, str]:
    """The full gate: window first, then pacing. Returns (go, reason)."""
    if force:
        return True, "forced"
    if not in_window(now_utc):
        return False, "outside Mon-Fri 8am ET – 5pm PT window"
    return pacing_ok(conn, channel, now_utc, rng, urgent=urgent)


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
    nuggets = db.nugget_candidates(conn)
    platinum = [n for n in nuggets if _is_platinum(n, today)]
    if platinum:
        return "platinum", _best_nugget(conn, platinum)
    if nuggets:
        return "nugget", _best_nugget(conn, nuggets)
    rfps = db.rfp_candidates(conn)  # open RFPs (silver), soonest deadline first
    silver_rfps = [r for r in rfps if str(r["lead_grade"]) == "silver"]
    if silver_rfps:
        return "rfp", silver_rfps[0]  # open RFP, soonest deadline
    bulletins_today = sum(
        1 for p in db.posts_today(conn, channel) if p["kind"] == "bulletin"
    )
    if bulletins_today < BULLETIN_MAX_PER_DAY:
        for cand in db.bulletin_candidates(conn):
            title = cand["title"] or ""
            if _BULLETIN_RELEVANT_RE.search(title) and not _BULLETIN_OFFTOPIC_RE.search(
                title
            ):
                return "bulletin", cand
    return None


def run_drip(
    client: WebClient | None,
    channel: str,
    conn: sqlite3.Connection,
    force: bool = False,
    dry_run: bool = False,
    rng: random.Random | None = None,
) -> str:
    """One cron tick: maybe post one thing. Returns a human-readable outcome."""
    rng = rng or random.Random()
    now = datetime.now(timezone.utc)
    choice = pick(conn, channel, now.date())
    if choice is None:
        return "skip: nothing new worth saying"
    kind, row = choice
    # A platinum (or exceptional gold) award may take the rare emergency second slot.
    urgent = kind in ("platinum", "nugget") and _is_exceptional(row, now.date())
    go, reason = should_post(conn, channel, now, rng, force=force, urgent=urgent)
    if not go:
        return f"skip: {reason}"
    if kind == "platinum":
        text, style = build_platinum(row)
    elif kind == "nugget":
        text, style = build_nugget(row)
    elif kind == "rfp":
        text, style = build_rfp_alert(row)
    else:
        text, style = build_bulletin(row)
    # Every proactive funding claim carries its source on a separate, safe line.
    text = text + source_line(row)
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
    except Exception as exc:  # noqa: BLE001 — timeout is ambiguous; never blind-retry
        db.finish_notification(conn, delivery_key, "unknown", error=type(exc).__name__)
        return (
            "unknown: Slack delivery could not be confirmed; Grant will not "
            "auto-retry this event to avoid a duplicate"
        )
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
    return f"posted {kind} ({style}) for lead #{row['id']}: {row['entity_name']}"
