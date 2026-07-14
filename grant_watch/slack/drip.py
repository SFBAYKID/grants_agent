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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

from .. import db, scoring

# Bulletin relevance: grants.gov phrase-search still lets through noise (live check
# 2026-07-13 surfaced 2011-era NSF programs). A bulletin must LOOK like our business.
_BULLETIN_RELEVANT_RE = re.compile(
    r"school|violence|security|surveillance|access control|cctv|hardening"
    r"|emergency|safety|svpp|cops", re.IGNORECASE)

DAILY_CAP = 3            # posts per day across both kinds (start slow, raise later)
MIN_GAP_MINUTES = 90     # never two posts closer than this
POST_PROBABILITY = 0.45  # per-eligible-tick chance — the "sporadic" in the spec
BULLETIN_MAX_PER_DAY = 1

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

_STATE_NAMES = {"WA": "Washington", "CA": "California", "MI": "Michigan",
                "PA": "Pennsylvania", "OR": "Oregon"}


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


def build_nugget(row: sqlite3.Row) -> tuple[str, str]:
    """(text, style_tag) for an award nugget. Styles are tagged so engagement points
    can tell us which phrasing actually gets responses. All facts from the row."""
    entity = row["entity_name"].title()
    state = _STATE_NAMES.get(row["state"] or "", row["state"] or "")
    amt = _fmt_amount(row["amount"])
    year = (row["funds_end"] or "")[:4]
    link = f"\n<{row['detail_url']}|award record>" if row["detail_url"] else ""
    styles = [
        ("ask-me", f"Hey team — {entity} in {state} just got {amt} for school "
                   f"security. Ask me here if you want it.{link}"),
        ("window", f"{entity} ({row['state']}) just landed {amt} in "
                   f"{row['program'] or 'security'} money — spend window runs through "
                   f"{year}. Details here if you want them.{link}"),
        ("worth-a-look", f"New one: {entity}, {state} — {amt} for "
                         f"{row['program'] or 'security'}. Worth a look if that's your "
                         f"territory.{link}"),
    ]
    style, text = random.choice(styles)
    return text, style


def build_bulletin(row: sqlite3.Row) -> tuple[str, str]:
    """(text, style_tag) for a program-news bulletin from a grants.gov row.
    Uses the opportunity TITLE (the news) with the posting agency as fallback."""
    what = (row["title"] or "").strip() or row["entity_name"] or "A federal program"
    close = f", closes {row['funds_end'][:10]}" if row["funds_end"] else ""
    link = f"\n<{row['detail_url']}|opportunity>" if row["detail_url"] else ""
    text = (f"Heads up — \"{what}\" application window is open{close}. "
            f"Worth mentioning to clients who'd apply.{link}")
    return text, "bulletin-open"


def pacing_ok(conn: sqlite3.Connection, channel: str, now_utc: datetime,
              rng: random.Random) -> tuple[bool, str]:
    """Cap + gap + jitter (window handled separately so each rule tests cleanly)."""
    today = db.posts_today(conn, channel)
    if len(today) >= DAILY_CAP:
        return False, f"daily cap reached ({DAILY_CAP})"
    if today:
        last = datetime.fromisoformat(today[-1]["posted_at"])
        gap_min = (now_utc - last).total_seconds() / 60
        if gap_min < MIN_GAP_MINUTES:
            return False, f"only {gap_min:.0f}m since last post (min {MIN_GAP_MINUTES}m)"
    if rng.random() > POST_PROBABILITY:
        return False, "jitter skip (keeps timing feeling human)"
    return True, "eligible"


def should_post(conn: sqlite3.Connection, channel: str, now_utc: datetime,
                rng: random.Random, force: bool = False) -> tuple[bool, str]:
    """The full gate: window first, then pacing. Returns (go, reason)."""
    if force:
        return True, "forced"
    if not in_window(now_utc):
        return False, "outside Mon-Fri 8am ET – 5pm PT window"
    return pacing_ok(conn, channel, now_utc, rng)


def pick(conn: sqlite3.Connection, channel: str) -> tuple[str, sqlite3.Row] | None:
    """Choose what to say: the top-scored unsurfaced GOLD nugget wins; a bulletin runs
    only when no nugget is available and today's bulletin slot is unused."""
    nuggets = db.nugget_candidates(conn)
    if nuggets:
        best = max(nuggets, key=lambda r: scoring.lead_score(
            r["program"], r["amount"], r["funds_start"] or ""))
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
    go, reason = should_post(conn, channel, now, rng, force=force)
    if not go:
        return f"skip: {reason}"
    choice = pick(conn, channel)
    if choice is None:
        return "skip: nothing new worth saying"
    kind, row = choice
    text, style = build_nugget(row) if kind == "nugget" else build_bulletin(row)
    if dry_run:
        return f"[dry-run] would post {kind} ({style}): {text}"
    resp = client.chat_postMessage(channel=channel, text=text, unfurl_links=False)
    db.record_post(conn, kind, int(row["id"]), channel, resp["ts"], style)
    db.mark_surfaced(conn, [int(row["id"])])
    return f"posted {kind} ({style}) for lead #{row['id']}: {row['entity_name']}"
