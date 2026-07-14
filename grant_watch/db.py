"""SQLite storage for grant_watch: the canonical 4-table schema from architectural.md.

Why: replaces the v1 flat `seen` table. Dedup key is UNIQUE(source, source_item_id) —
`source` includes the CFDA suffix (e.g. 'usaspending:16.071') because SVPP spans two
CFDA codes and collapsing them would collide/duplicate awards (docs/FINDINGS.md).
Phase 4 swaps this file's connection for DigitalOcean Postgres with the same schema.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Lead, RunStats

# Default DB lives next to the repo root; git-ignored (*.db).
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "grant_watch.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- 'usaspending:16.071', 'grants.gov', 'webs', ...
  source_item_id TEXT NOT NULL,
  lead_grade TEXT CHECK(lead_grade IN ('gold','silver','watch')),
  entity_name TEXT NOT NULL,
  title TEXT,                      -- source item title (opportunity name / award desc)
  entity_type TEXT,                -- district, city, nonpublic_school, nonprofit
  state TEXT, county TEXT,
  program TEXT,                    -- SVPP, NSGP, CSSGP, PCCD, STOP, RFP:<platform>
  amount REAL,
  funds_start DATE, funds_end DATE,
  detail_url TEXT,
  raw_json TEXT,
  first_seen TIMESTAMP, last_seen TIMESTAMP,
  status TEXT DEFAULT 'new',       -- new, surfaced, contacted, snoozed, replied, opportunity, dead
  status_note TEXT,                -- e.g. the human's [Bad lead] reason — feeds scoring later
  UNIQUE(source, source_item_id)
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER REFERENCES leads(id),
  name TEXT, title TEXT, email TEXT, phone TEXT,
  source_url TEXT, confidence TEXT CHECK(confidence IN ('high','medium','low')),
  contact_status TEXT DEFAULT 'unverified'   -- unverified, verified, not_found (NEVER fabricated)
);
CREATE TABLE IF NOT EXISTS outreach (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER, contact_id INTEGER,
  channel TEXT, draft TEXT, approved_by TEXT,  -- approved_by REQUIRED before sent_at is set
  sent_at TIMESTAMP, response TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started TIMESTAMP, finished TIMESTAMP,
  source TEXT, items_seen INT, items_new INT, errors TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  -- every message Grant proactively posts (drip nuggets + bulletins); the thread
  -- anchor for conversation and the unit engagement points attach to
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('nugget','bulletin')),
  lead_id INTEGER REFERENCES leads(id),
  channel TEXT NOT NULL,
  ts TEXT NOT NULL,                -- Slack message ts (thread anchor)
  style TEXT,                      -- template tag, so engagement can tune phrasing
  posted_at TIMESTAMP,
  UNIQUE(channel, ts)
);
CREATE TABLE IF NOT EXISTS engagement (
  -- +1 point each time a HUMAN interacts with a Grant post; deduped per
  -- (post, user, kind). Grant is incentivized to earn these — never by lying.
  id INTEGER PRIMARY KEY,
  post_id INTEGER REFERENCES posts(id),
  slack_user TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('reply','reaction','claim','question')),
  at TIMESTAMP,
  UNIQUE(post_id, slack_user, kind)
);
"""


def _now() -> str:
    """UTC ISO timestamp — one format everywhere so Postgres migration is painless."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (and create if needed) the database with the canonical schema.

    Also applies tiny in-place migrations for DBs created before a column existed —
    SQLite has no IF NOT EXISTS for columns, so we check PRAGMA table_info.
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row  # dict-style access for Slack formatting code
    # WAL + busy timeout: the bot, the drip cron, and pollers can now write
    # concurrently (architectural-critic M8).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    lead_cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
    if "status_note" not in lead_cols:  # migration: added in Phase 3 for [Bad lead] reasons
        conn.execute("ALTER TABLE leads ADD COLUMN status_note TEXT")
    if "assigned_to" not in lead_cols:  # migration: drip-engine claim/ownership
        conn.execute("ALTER TABLE leads ADD COLUMN assigned_to TEXT")
        conn.execute("ALTER TABLE leads ADD COLUMN assigned_at TIMESTAMP")
    if "title" not in lead_cols:  # migration: bulletins need the opportunity title
        conn.execute("ALTER TABLE leads ADD COLUMN title TEXT")
    conn.commit()
    return conn


def upsert_lead(conn: sqlite3.Connection, lead: Lead) -> bool:
    """Insert a lead, or refresh last_seen if already known.

    Returns True only when the lead is NEW (i.e., digest-worthy). Dedup rides on the
    UNIQUE(source, source_item_id) constraint rather than a pre-select, so two
    overlapping runs cannot double-insert.
    """
    it = lead.item
    now = _now()
    try:
        conn.execute(
            """INSERT INTO leads (source, source_item_id, lead_grade, entity_name,
                                  title, entity_type, state, program, amount,
                                  funds_start, funds_end, detail_url, raw_json,
                                  first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it.source, str(it.item_id), lead.grade.value, it.entity, it.title,
             lead.entity_type, it.state, it.program, it.amount, it.start or None,
             it.end or None, it.url, it.raw_json(), now, now),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Known item: refresh last_seen, and backfill title for rows stored before
        # the title column existed (source data, not invention).
        conn.execute(
            "UPDATE leads SET last_seen = ?, title = COALESCE(title, ?) "
            "WHERE source = ? AND source_item_id = ?",
            (now, it.title or None, it.source, str(it.item_id)),
        )
        conn.commit()
        return False


def log_run(conn: sqlite3.Connection, started: str, stats: RunStats) -> None:
    """Record one source's poll outcome in `runs` (started passed in by the caller
    so all sources in a run share one start stamp)."""
    conn.execute(
        "INSERT INTO runs (started, finished, source, items_seen, items_new, errors) "
        "VALUES (?,?,?,?,?,?)",
        (started, _now(), stats.source, stats.items_seen, stats.items_new, stats.errors),
    )
    conn.commit()


def seed_from_csv(conn: sqlite3.Connection, csv_path: Path) -> tuple[int, int]:
    """Seed `leads` from data/svpp_active_awards_CA_MI_PA_WA.csv (75 verified GOLD
    awards pulled live 2026-07-13 — docs/FINDINGS.md).

    The CSV has no award ids, so source_item_id is a deterministic slug of
    recipient+fy_cohort; re-seeding is therefore idempotent. Returns (rows, new).
    """
    rows = new = 0
    now = _now()
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            rows += 1
            slug = f"{rec['recipient'].lower().replace(' ', '_')}~{rec['fy_cohort']}"
            try:
                conn.execute(
                    """INSERT INTO leads (source, source_item_id, lead_grade, entity_name,
                                          state, program, amount, funds_start, funds_end,
                                          raw_json, first_seen, last_seen)
                       VALUES ('seed:svpp_csv', ?, 'gold', ?, ?, 'SVPP', ?, ?, ?, '{}', ?, ?)""",
                    (slug, rec["recipient"], rec["state"], float(rec["award_amount"]),
                     rec["start_date"], rec["end_date"], now, now),
                )
                new += 1
            except sqlite3.IntegrityError:
                pass  # already seeded — idempotent
    conn.commit()
    return rows, new


def status_summary(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(source, grade, count) rows for the CLI status command."""
    return list(conn.execute(
        "SELECT source, lead_grade, COUNT(*) FROM leads GROUP BY source, lead_grade "
        "ORDER BY source, lead_grade"
    ))


def reconcile_seed_duplicates(conn: sqlite3.Connection) -> int:
    """Retire seed-CSV rows that a live poller row has superseded.

    Why: the 2026-07-13 live digest showed the same award twice — once from
    'seed:svpp_csv' (no award id, no URL) and once from live USASpending. Match is
    EXACT on normalized entity + amount + funds_end (verified 75/75 seed rows matched
    this way with zero false lonelies). The live row wins (it carries the award id and
    deep link); the seed row goes to status='dead' with an explanatory note, preserving
    history. Returns how many seed rows were retired. Idempotent.
    """
    cur = conn.execute("""
        UPDATE leads SET status = 'dead',
               status_note = 'superseded by live award row (same entity/amount/window)'
        WHERE source = 'seed:svpp_csv' AND status != 'dead' AND EXISTS (
            SELECT 1 FROM leads l
            WHERE l.source LIKE 'usaspending:%'
              AND UPPER(TRIM(l.entity_name)) = UPPER(TRIM(leads.entity_name))
              AND l.amount = leads.amount
              AND l.funds_end = leads.funds_end)""")
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- Phase 3: Slack workflow

def digest_leads(conn: sqlite3.Connection, expiring_days: int = 90
                 ) -> dict[str, list[sqlite3.Row]]:
    """Rows for the weekly digest, in three buckets:
      gold      new GOLD leads not yet surfaced (freshest start date first, then $)
      silver    new SILVER leads not yet surfaced
      expiring  GOLD leads whose spend window ends within `expiring_days`
                (use-it-or-lose-it — regardless of surfaced status, but not dead/contacted)
    """
    gold = list(conn.execute(
        "SELECT * FROM leads WHERE lead_grade='gold' AND status='new' "
        "ORDER BY funds_start DESC, amount DESC"))
    silver = list(conn.execute(
        "SELECT * FROM leads WHERE lead_grade='silver' AND status='new' "
        "ORDER BY funds_end ASC"))
    expiring = list(conn.execute(
        "SELECT * FROM leads WHERE lead_grade='gold' "
        "AND status NOT IN ('dead','contacted') "
        "AND funds_end IS NOT NULL "
        "AND date(funds_end) BETWEEN date('now') AND date('now', ?) "
        "ORDER BY funds_end ASC", (f"+{expiring_days} days",)))
    return {"gold": gold, "silver": silver, "expiring": expiring}


def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    """One lead row by primary key (None when the id is stale/unknown)."""
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def set_lead_status(conn: sqlite3.Connection, lead_id: int, status: str,
                    note: str | None = None) -> None:
    """Move a lead through the triage workflow (surfaced/contacted/snoozed/dead...).
    `note` records the human's reason (e.g. [Bad lead] feedback for future scoring)."""
    conn.execute("UPDATE leads SET status = ?, status_note = COALESCE(?, status_note) "
                 "WHERE id = ?", (status, note, lead_id))
    conn.commit()


def mark_surfaced(conn: sqlite3.Connection, lead_ids: list[int]) -> None:
    """Flip a batch of just-posted digest leads from 'new' to 'surfaced'."""
    conn.executemany("UPDATE leads SET status='surfaced' WHERE id=? AND status='new'",
                     [(i,) for i in lead_ids])
    conn.commit()


def create_outreach(conn: sqlite3.Connection, lead_id: int, draft: str) -> int:
    """Store a proposed email draft (channel='slack-thread'). Returns outreach id.
    approved_by/sent_at stay NULL until a human explicitly approves (Constitution 10)."""
    cur = conn.execute(
        "INSERT INTO outreach (lead_id, channel, draft) VALUES (?, 'slack-thread', ?)",
        (lead_id, draft))
    conn.commit()
    return int(cur.lastrowid)


def approve_outreach(conn: sqlite3.Connection, outreach_id: int, approver: str) -> None:
    """Record the human approval + the moment we handed the send to @Persequor.
    sent_at here means 'handed off', not 'delivered' — Persequor owns actual delivery."""
    conn.execute("UPDATE outreach SET approved_by = ?, sent_at = ? WHERE id = ?",
                 (approver, _now(), outreach_id))
    conn.commit()


# ---------------------------------------------------------------- drip engine + claims

def claim_lead(conn: sqlite3.Connection, lead_id: int, slack_user: str) -> bool:
    """First-click ownership. Race-safe conditional UPDATE: exactly one claimer wins
    (architectural-critic-approved primitive). Dead/snoozed leads can't be claimed."""
    cur = conn.execute(
        "UPDATE leads SET assigned_to = ?, assigned_at = ? "
        "WHERE id = ? AND assigned_to IS NULL AND status NOT IN ('dead','snoozed')",
        (slack_user, _now(), lead_id))
    conn.commit()
    return cur.rowcount == 1


def record_post(conn: sqlite3.Connection, kind: str, lead_id: int | None,
                channel: str, ts: str, style: str) -> int:
    """Log a proactive Grant post (the thread anchor engagement attaches to)."""
    cur = conn.execute(
        "INSERT INTO posts (kind, lead_id, channel, ts, style, posted_at) "
        "VALUES (?,?,?,?,?,?)", (kind, lead_id, channel, ts, style, _now()))
    conn.commit()
    return int(cur.lastrowid)


def find_post_by_ts(conn: sqlite3.Connection, channel: str, ts: str) -> sqlite3.Row | None:
    """Look up a Grant post from a thread anchor ts (to attribute engagement)."""
    return conn.execute("SELECT * FROM posts WHERE channel = ? AND ts = ?",
                        (channel, ts)).fetchone()


def record_engagement(conn: sqlite3.Connection, post_id: int, slack_user: str,
                      kind: str) -> bool:
    """+1 point when a human interacts with a post. Deduped per (post, user, kind)
    so one enthusiastic user can't inflate the score. Returns True if new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO engagement (post_id, slack_user, kind, at) "
        "VALUES (?,?,?,?)", (post_id, slack_user, kind, _now()))
    conn.commit()
    return cur.rowcount == 1


def engagement_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Grant's score: total points + per-kind breakdown (the tuning signal)."""
    stats = {"total": conn.execute("SELECT COUNT(*) FROM engagement").fetchone()[0]}
    for kind, n in conn.execute(
            "SELECT kind, COUNT(*) FROM engagement GROUP BY kind"):
        stats[kind] = n
    return stats


def posts_today(conn: sqlite3.Connection, channel: str) -> list[sqlite3.Row]:
    """Today's proactive posts (UTC day) — the daily-cap input."""
    return list(conn.execute(
        "SELECT * FROM posts WHERE channel = ? AND date(posted_at) = date('now') "
        "ORDER BY posted_at", (channel,)))


def nugget_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unsurfaced GOLD leads eligible for a drip nugget."""
    return list(conn.execute(
        "SELECT * FROM leads WHERE lead_grade='gold' AND status='new'"))


def bulletin_candidates(conn: sqlite3.Connection, max_age_days: int = 14
                        ) -> list[sqlite3.Row]:
    """Fresh grants.gov opportunities not yet posted as a bulletin — program-level
    news ('application window just opened'), soonest close date first."""
    return list(conn.execute(
        "SELECT * FROM leads WHERE source = 'grants.gov' "
        "AND first_seen >= datetime('now', ?) "
        "AND id NOT IN (SELECT lead_id FROM posts WHERE lead_id IS NOT NULL) "
        "AND funds_end != '' AND date(funds_end) >= date('now') "
        "ORDER BY date(funds_end) ASC", (f"-{max_age_days} days",)))
