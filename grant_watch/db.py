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
  entity_type TEXT,                -- district, city, nonpublic_school, nonprofit
  state TEXT, county TEXT,
  program TEXT,                    -- SVPP, NSGP, CSSGP, PCCD, STOP, RFP:<platform>
  amount REAL,
  funds_start DATE, funds_end DATE,
  detail_url TEXT,
  raw_json TEXT,
  first_seen TIMESTAMP, last_seen TIMESTAMP,
  status TEXT DEFAULT 'new',       -- new, surfaced, contacted, replied, opportunity, dead
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
"""


def _now() -> str:
    """UTC ISO timestamp — one format everywhere so Postgres migration is painless."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (and create if needed) the database with the canonical schema."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
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
                                  entity_type, state, program, amount, funds_start,
                                  funds_end, detail_url, raw_json, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it.source, str(it.item_id), lead.grade.value, it.entity, lead.entity_type,
             it.state, it.program, it.amount, it.start or None, it.end or None,
             it.url, it.raw_json(), now, now),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE leads SET last_seen = ? WHERE source = ? AND source_item_id = ?",
            (now, it.source, str(it.item_id)),
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
