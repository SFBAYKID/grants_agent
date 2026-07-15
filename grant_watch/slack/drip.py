"""The drip engine: Grant surfaces one golden nugget at a time, sounding human.

Chase's spec (2026-07-13): structured underneath (best lead_score first), sporadic on
the surface (jittered timing, never a wall of leads). Short messages — two sentences
max, help-first, little-to-no emoji. Two kinds:
  nugget    an entity that just WON money   ("Castle Rock SD just got $500K...")
  bulletin  program-level news from grants.gov ("SVPP window just opened, closes 8/4")

Run via cron every ~30 min; each tick decides for itself whether to speak:
  in the window? (Mon-Fri, 8:00 America/New_York through 17:00 America/Los_Angeles)
  under the daily cap? past the min gap? and a random skip so timing feels human.
Links: only real ones we actually hold (the award's source record). No invented URLs.
"""

from __future__ import annotations

import random
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any  # Slack Block Kit payloads are runtime-shaped mappings.
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

from .. import db, scoring

# Bulletin relevance: grants.gov phrase-search still lets through noise (live check
# 2026-07-13 surfaced 2011-era NSF programs). A bulletin must LOOK like our business.
_BULLETIN_RELEVANT_RE = re.compile(
    r"school|violence|security|surveillance|access control|cctv|hardening"
    r"|emergency|safety|svpp|cops", re.IGNORECASE)

DAILY_AIM = 2            # normal target; jitter decides whether a third is worthwhile
DAILY_CAP = 3            # normal hard cap across both kinds
ABSOLUTE_CAP = 4         # rare fourth only for a newly dated exceptional event
MIN_GAP_MINUTES = 90     # never two posts closer than this
POST_PROBABILITY = 0.45  # per-eligible-tick chance — the "sporadic" in the spec
BULLETIN_MAX_PER_DAY = 1

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

_STATE_NAMES = {"WA": "Washington", "CA": "California", "MI": "Michigan",
                "PA": "Pennsylvania", "OR": "Oregon"}

_LEAD_ACTIONS = (
    ("grant_draft_email", "✉️ Draft email"),
    ("grant_mark_contacted", "✅ Mark contacted"),
    ("grant_snooze", "💤 Snooze"),
    ("grant_bad_lead", "👎 Bad lead"),
)


def in_window(now_utc: datetime) -> bool:
    """Mon-Fri, from 8:00 Eastern until 17:00 Pacific (per Chase)."""
    et, pt = now_utc.astimezone(ET), now_utc.astimezone(PT)
    return et.weekday() < 5 and et.hour >= 8 and pt.hour < 17


def _fmt_amount(amount: float | None) -> str:
    """$500K / $1.2M style — short, the way a person would say it."""
    if not amount or amount <= 0:
        return ""
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M".replace(".0M", "M")
    return f"${amount / 1_000:.0f}K"


def _row_value(row: sqlite3.Row, key: str) -> object | None:
    """Read an optional joined column without requiring it in unit-level lead rows."""
    return row[key] if key in row.keys() else None


def _salesforce_context(row: sqlite3.Row) -> str:
    """Render one concise, persisted read-only CRM link when a high match exists."""
    opportunity_link = str(_row_value(row, "salesforce_opportunity_link") or "")
    if opportunity_link:
        name = str(_row_value(row, "salesforce_opportunity_name") or "open Opportunity")
        owner = str(_row_value(row, "salesforce_opportunity_owner") or "")
        owner_text = f", owned by {owner}" if owner else ""
        return f"\nSalesforce: <{opportunity_link}|{name}>{owner_text}."
    account_link = str(_row_value(row, "salesforce_account_link") or "")
    if account_link:
        owner = str(_row_value(row, "salesforce_account_owner") or "")
        owner_text = f", owned by {owner}" if owner else ""
        return f"\nSalesforce: <{account_link}|existing Account>{owner_text}."
    return ""


def build_nugget(row: sqlite3.Row) -> tuple[str, str]:
    """(text, style_tag) for an award nugget. Styles are tagged so engagement points
    can tell us which phrasing actually gets responses. All facts from the row."""
    entity = row["entity_name"].title()
    state = _STATE_NAMES.get(row["state"] or "", row["state"] or "")
    amt = _fmt_amount(row["amount"])
    year = (row["funds_end"] or "")[:4]
    link = f"\n<{row['detail_url']}|award record>" if row["detail_url"] else ""
    event_date = row["current_event_occurred_on"] or ""
    source_name = "USAspending" if str(row["source"]).startswith("usaspending:") \
        else "The source record"
    evidence = (f"{source_name} records an award event dated {event_date}"
                if event_date else f"{source_name} lists an award")
    amount_phrase = f" for {amt}" if amt else ""
    amount_with = f" with {amt}" if amt else ""
    window_phrase = f"; its spend window runs through {year}" if year else ""
    salesforce_context = _salesforce_context(row)
    styles = [
        ("ask-me", f"{evidence} for {entity} in {state}{amount_phrase}{window_phrase}. "
                   f"Ask me here if you want the details.{link}{salesforce_context}"),
        ("window", f"Award record worth a look: {entity} ({row['state']}){amount_phrase} "
                   f"in {row['program'] or 'security'} funding{window_phrase}."
                   f"{link}{salesforce_context}"),
        ("worth-a-look", f"{source_name} lists {entity}, {state}{amount_with} in "
                         f"{row['program'] or 'security'} funding{window_phrase}. Worth a look "
                         f"if that's your territory.{link}{salesforce_context}"),
    ]
    style, text = random.choice(styles)
    return text, style


def build_bulletin(row: sqlite3.Row) -> tuple[str, str]:
    """Build truthful program news from an official opportunity record.

    The opportunity title is the news, with the posting agency as fallback.
    """
    what = (row["title"] or "").strip() or row["entity_name"] or "A federal program"
    close = f", closes {row['funds_end'][:10]}" if row["funds_end"] else ""
    link = f"\n<{row['detail_url']}|opportunity>" if row["detail_url"] else ""
    source_name = ("California Grants Portal"
                   if row["source"] == "ca-grants-portal" else "Grants.gov")
    text = (f"Heads up — {source_name} lists \"{what}\" as open{close}. "
            f"Worth mentioning to clients who'd apply.{link}")
    return text, "bulletin-open"


def build_lead_blocks(text: str, lead_id: int) -> list[dict[str, Any]]:
    """Attach per-lead workflow actions to one proactive lead notification.

    The payload intentionally contains one lead only; Grant never combines alerts
    into a multi-lead digest.
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "block_id": f"lead-{lead_id}", "elements": [
            {"type": "button", "action_id": action_id,
             "text": {"type": "plain_text", "text": label, "emoji": True},
             "value": str(lead_id)}
            for action_id, label in _LEAD_ACTIONS
        ]},
    ]


def pacing_ok(conn: sqlite3.Connection, channel: str, now_utc: datetime,
              rng: random.Random, urgent: bool = False) -> tuple[bool, str]:
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
            return False, f"only {gap_min:.0f}m since last post (min {MIN_GAP_MINUTES}m)"
    probability = POST_PROBABILITY if len(today) < DAILY_AIM else 0.25
    if not urgent and rng.random() > probability:
        return False, "jitter skip (keeps timing feeling human)"
    return True, "eligible"


def should_post(conn: sqlite3.Connection, channel: str, now_utc: datetime,
                rng: random.Random, force: bool = False,
                urgent: bool = False) -> tuple[bool, str]:
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


def pick(conn: sqlite3.Connection, channel: str) -> tuple[str, sqlite3.Row] | None:
    """Choose what to say: the top-scored unsurfaced GOLD nugget wins; a bulletin runs
    only when no nugget is available and today's bulletin slot is unused."""
    nuggets = db.nugget_candidates(conn)
    if nuggets:
        best = max(nuggets, key=lambda r: (
            2 if r["salesforce_opportunity_link"] else
            1 if r["salesforce_account_link"] else 0,
            scoring.lead_score(
                r["program"], r["amount"], r["current_event_occurred_on"] or "")
            * scoring.feedback_multiplier(
                db.program_outcome_points(conn, r["program"] or "")),
        ))
        return "nugget", best
    bulletins_today = sum(1 for p in db.posts_today(conn, channel)
                          if p["kind"] == "bulletin")
    if bulletins_today < BULLETIN_MAX_PER_DAY:
        for cand in db.bulletin_candidates(conn):
            if _BULLETIN_RELEVANT_RE.search(cand["title"] or ""):
                return "bulletin", cand
    return None


def run_drip(client: WebClient | None, channel: str, conn: sqlite3.Connection,
             force: bool = False, dry_run: bool = False,
             rng: random.Random | None = None) -> str:
    """One cron tick: maybe post one thing. Returns a human-readable outcome."""
    rng = rng or random.Random()
    now = datetime.now(timezone.utc)
    choice = pick(conn, channel)
    if choice is None:
        return "skip: nothing new worth saying"
    kind, row = choice
    urgent = kind == "nugget" and _is_exceptional(row, now.date())
    go, reason = should_post(conn, channel, now, rng, force=force, urgent=urgent)
    if not go:
        return f"skip: {reason}"
    text, style = build_nugget(row) if kind == "nugget" else build_bulletin(row)
    if dry_run:
        return f"[dry-run] would post {kind} ({style}): {text}"
    event_id = int(row["current_event_id"]) if row["current_event_id"] else None
    delivery_key = db.reserve_notification(
        conn, int(row["id"]), event_id, channel, kind,
        {"text": text, "style": style, "urgent": urgent})
    if delivery_key is None:
        return "skip: this funding event is already reserved or delivered"
    assert client is not None
    try:
        if kind == "nugget":
            resp = client.chat_postMessage(
                channel=channel, text=text,
                blocks=build_lead_blocks(text, int(row["id"])),
                unfurl_links=False)
        else:
            resp = client.chat_postMessage(
                channel=channel, text=text, unfurl_links=False)
    except Exception as exc:  # noqa: BLE001 — timeout is ambiguous; never blind-retry
        db.finish_notification(
            conn, delivery_key, "unknown", error=type(exc).__name__)
        return ("unknown: Slack delivery could not be confirmed; Grant will not "
                "auto-retry this event to avoid a duplicate")
    db.record_post(conn, kind, int(row["id"]), channel, resp["ts"], style,
                   delivery_key=delivery_key, event_id=event_id, urgent=urgent)
    db.finish_notification(conn, delivery_key, "delivered", slack_ts=resp["ts"])
    db.mark_surfaced(conn, [int(row["id"])])
    return f"posted {kind} ({style}) for lead #{row['id']}: {row['entity_name']}"
