"""SQLite persistence for Grant's current projections and immutable event history.

The ``leads`` table remains the compatibility projection used by Slack and search;
``source_observations`` and ``funding_events`` preserve what changed and which claims
are evidence-backed. Schema transitions live in migrations.py rather than connect().
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .migrations import apply_migrations
from .models import (
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    RunStats,
    VerificationStatus,
)

# Default DB lives next to the repo root; git-ignored (*.db).
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "grant_watch.db"
_LEAD_EVENT_SELECT = """l.*, e.event_type AS current_event_type,
    e.occurred_on AS current_event_occurred_on,
    e.date_precision AS current_event_date_precision,
    e.verification_status AS current_event_verification_status,
    e.backfill AS current_event_backfill,
    e.suppressed AS current_event_suppressed"""
_CRM_CONTEXT_SELECT = """
    (SELECT s.status FROM salesforce_lookup_state s
     WHERE s.lead_id=l.id) AS salesforce_status,
    (SELECT m.link FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Opportunity'
       AND m.confidence='high' AND COALESCE(m.is_closed,0)=0
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_opportunity_link,
    (SELECT m.name FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Opportunity'
       AND m.confidence='high' AND COALESCE(m.is_closed,0)=0
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_opportunity_name,
    (SELECT m.owner FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Opportunity'
       AND m.confidence='high' AND COALESCE(m.is_closed,0)=0
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_opportunity_owner,
    (SELECT m.link FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Account' AND m.confidence='high'
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_account_link,
    (SELECT m.owner FROM salesforce_matches m
     JOIN salesforce_lookup_state s ON s.lead_id=m.lead_id
     WHERE m.lead_id=l.id AND m.sobject='Account' AND m.confidence='high'
       AND s.status='found' AND datetime(s.checked_at) >= datetime('now','-24 hours')
     ORDER BY m.record_id LIMIT 1) AS salesforce_account_owner"""


def _now() -> str:
    """UTC ISO timestamp — one format everywhere so Postgres migration is painless."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a writable database and apply explicit versioned migrations."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row  # dict-style access for Slack formatting code
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    apply_migrations(conn)
    return conn


def connect_readonly(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open an existing SQLite database without migrations, WAL, or write access.

    Dry-run commands use this path so observing a proposed Slack/export/outreach
    action cannot create a database, advance schema, or alter journal sidecars.
    """
    resolved = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA query_only=ON")
    return conn


def canonical_entity_key(entity: str, state: str = "") -> str:
    """Return a conservative entity key for grouping exact organization repeats.

    This is not fuzzy identity resolution. It only normalizes punctuation, whitespace,
    case, and the state suffix so cross-event batching cannot duplicate the same name.
    """
    normalized = re.sub(r"[^a-z0-9]+", " ", entity.lower()).strip()
    return f"{normalized}|{state.strip().upper()}"


def upsert_lead(conn: sqlite3.Connection, lead: Lead) -> bool:
    """Project one source item and append evidence when its facts changed.

    Returns true for a new lead or a substantive, non-backfill event. Re-fetching an
    unchanged source item only refreshes ``last_seen`` and cannot create another alert.
    """
    it = lead.item
    now = _now()
    raw_json = it.raw_json()
    payload_hash = it.observation_hash()
    existing = conn.execute(
        "SELECT * FROM leads WHERE source=? AND source_item_id=?",
        (it.source, str(it.item_id)),
    ).fetchone()
    inserted = existing is None
    if inserted:
        conn.execute(
            """INSERT INTO leads (source, source_item_id, lead_grade, entity_name,
                                  title, entity_type, state, program, amount,
                                  funds_start, funds_end, detail_url, raw_json,
                                  first_seen, last_seen, canonical_entity_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it.source, str(it.item_id), lead.grade.value, it.entity, it.title,
             lead.entity_type, it.state, it.program, it.amount, it.start or None,
             it.end or None, it.url, raw_json, now, now,
             canonical_entity_key(it.entity, it.state)),
        )
        existing = conn.execute(
            "SELECT * FROM leads WHERE source=? AND source_item_id=?",
            (it.source, str(it.item_id)),
        ).fetchone()
    assert existing is not None  # inserted above or selected before the transaction

    incoming = (
        lead.grade.value, it.entity, it.title, lead.entity_type, it.state, it.program,
        it.amount, it.start or None, it.end or None, it.url, raw_json,
        canonical_entity_key(it.entity, it.state),
    )
    suppressed = it.backfill or it.event_type == FundingEventType.RECORD_OBSERVED

    lead_id = int(existing["id"])
    conn.execute(
        """INSERT OR IGNORE INTO source_observations
             (lead_id, source, source_item_id, observed_at, payload_hash, raw_json,
              source_url, source_locator, verification_status)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (lead_id, it.source, str(it.item_id), now, payload_hash, raw_json, it.url,
         it.source_locator, it.verification_status.value),
    )
    observation = conn.execute(
        """SELECT id FROM source_observations
           WHERE source=? AND source_item_id=? AND payload_hash=?""",
        (it.source, str(it.item_id), payload_hash),
    ).fetchone()
    assert observation is not None

    event_insert = conn.execute(
        """INSERT OR IGNORE INTO funding_events
             (lead_id, observation_id, event_type, occurred_on, date_precision,
              amount, funded_scope, eligible_scope, application_portal,
              evidence_excerpt, evidence_hash, source_url, source_locator,
              verification_status, backfill, suppressed, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (lead_id, int(observation["id"]), it.event_type.value,
         it.event_date or None, it.date_precision.value, it.amount,
         it.funded_scope, it.eligible_scope, it.application_portal,
         it.evidence_excerpt or it.title, payload_hash, it.url,
         it.source_locator, it.verification_status.value, int(it.backfill),
         int(suppressed), now),
    )
    event_created = event_insert.rowcount == 1
    if event_created:
        conn.execute("UPDATE leads SET current_event_id=? WHERE id=?",
                     (int(event_insert.lastrowid), lead_id))
    if not inserted:
        conn.execute(
            """UPDATE leads SET lead_grade=?, entity_name=?, title=?, entity_type=?,
                      state=?, program=?, amount=?, funds_start=?, funds_end=?,
                      detail_url=?, raw_json=?, last_seen=?, canonical_entity_key=?,
                      status=CASE WHEN ? AND ?=0 THEN 'new' ELSE status END
                 WHERE id=?""",
            (*incoming[:10], incoming[10], now, incoming[11], int(event_created),
             int(suppressed), lead_id),
        )
    if inserted:
        conn.execute("UPDATE leads SET last_seen=? WHERE id=?", (now, lead_id))
    conn.commit()
    # Projection-only metadata changes deliberately do not drive notifications.
    return inserted or (event_created and not suppressed)


def log_run(conn: sqlite3.Connection, started: str, stats: RunStats) -> None:
    """Record one source's poll outcome in `runs` (started passed in by the caller
    so all sources in a run share one start stamp)."""
    conn.execute(
        """INSERT INTO runs
             (started, finished, source, items_seen, items_new, errors, complete, error_code)
           VALUES (?,?,?,?,?,?,?,?)""",
        (started, _now(), stats.source, stats.items_seen, stats.items_new, stats.errors,
         int(stats.complete), stats.error_code),
    )
    conn.commit()


def acquire_poll_lock(conn: sqlite3.Connection, name: str, owner: str,
                      stale_hours: int = 2) -> bool:
    """Acquire a named poll lock, clearing only locks older than ``stale_hours``."""
    with conn:
        conn.execute(
            "DELETE FROM poll_locks WHERE name=? AND acquired_at < datetime('now', ?)",
            (name, f"-{stale_hours} hours"),
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO poll_locks(name,owner,acquired_at) VALUES (?,?,?)",
            (name, owner, _now()),
        )
    return cur.rowcount == 1


def release_poll_lock(conn: sqlite3.Connection, name: str, owner: str) -> None:
    """Release only the caller's lock so one process cannot unlock another."""
    with conn:
        conn.execute("DELETE FROM poll_locks WHERE name=? AND owner=?", (name, owner))


def seed_from_csv(conn: sqlite3.Connection, csv_path: Path) -> tuple[int, int]:
    """Seed `leads` from data/svpp_active_awards_CA_MI_PA_WA.csv (75 verified GOLD
    awards pulled live 2026-07-13 — docs/FINDINGS.md).

    The CSV has no award ids, so source_item_id is a deterministic slug of
    recipient+fy_cohort; re-seeding is therefore idempotent. Returns (rows, new).
    """
    rows = new = 0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            rows += 1
            slug = f"{rec['recipient'].lower().replace(' ', '_')}~{rec['fy_cohort']}"
            inserted = upsert_lead(conn, Lead(
                item=RawItem(
                    source="seed:svpp_csv",
                    item_id=slug,
                    title="Historical SVPP award record",
                    entity=rec["recipient"],
                    state=rec["state"],
                    program="SVPP",
                    amount=float(rec["award_amount"]),
                    start=rec["start_date"],
                    end=rec["end_date"],
                    url="",
                    raw={"fy_cohort": rec["fy_cohort"]},
                    event_type=FundingEventType.RECORD_OBSERVED,
                    verification_status=VerificationStatus.VERIFIED,
                    backfill=True,
                ),
                grade=LeadGrade.GOLD,
            ))
            if inserted:
                new += 1
    return rows, new


def status_summary(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(source, grade, count) rows for the CLI status command."""
    return list(conn.execute(
        "SELECT source, lead_grade, COUNT(*) FROM leads GROUP BY source, lead_grade "
        "ORDER BY source, lead_grade"
    ))


def save_search_request(conn: sqlite3.Connection, session_key: str, requested_by: str,
                        filters: dict[str, object], scope: str, top_n: int | None,
                        format_name: str, lead_ids: list[int]) -> str:
    """Persist one immutable completed search snapshot for follow-up CRM/export actions."""
    request_id = str(uuid.uuid4())
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO search_requests
                 (id,session_key,requested_by,filters_json,scope,top_n,format,state,
                  result_lead_ids_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'complete',?,?,?)""",
            (request_id, session_key, requested_by,
             json.dumps(filters, sort_keys=True, default=str), scope, top_n,
             format_name or None, json.dumps(lead_ids), now, now),
        )
    return request_id


def get_search_request(conn: sqlite3.Connection, request_id: str,
                       requested_by: str) -> sqlite3.Row | None:
    """Return a completed search snapshot only to its initiating Slack user."""
    return conn.execute(
        "SELECT * FROM search_requests WHERE id=? AND requested_by=? AND state='complete'",
        (request_id, requested_by),
    ).fetchone()


def create_export_job(conn: sqlite3.Connection, requested_by: str,
                      format_name: str, idempotency_key: str,
                      search_request_id: str | None = None) -> str:
    """Persist an export attempt before artifact creation or external API calls."""
    job_id = str(uuid.uuid4())
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO export_jobs
                 (id,search_request_id,requested_by,format,idempotency_key,state,
                  created_at,updated_at)
               VALUES (?,?,?,?,?,'creating',?,?)""",
            (job_id, search_request_id, requested_by, format_name,
             idempotency_key, now, now),
        )
    return job_id


def finish_export_job(conn: sqlite3.Connection, job_id: str, state: str,
                      url: str = "", external_id: str = "",
                      error: str = "") -> None:
    """Record the final truthful export state and any recoverable external locator."""
    allowed = {"created", "fallback_excel", "failed"}
    if state not in allowed:
        raise ValueError(f"unsupported export job state '{state}'")
    with conn:
        conn.execute(
            """UPDATE export_jobs SET state=?,url=?,external_id=?,error=?,updated_at=?
               WHERE id=?""",
            (state, url or None, external_id or None, error or None, _now(), job_id),
        )


def reconcile_seed_duplicates(conn: sqlite3.Connection) -> int:
    """Retire seed-CSV rows that a live poller row has superseded.

    Why: the 2026-07-13 live output showed the same award twice — once from
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

def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    """One lead row by primary key (None when the id is stale/unknown)."""
    return conn.execute(
        f"""SELECT {_LEAD_EVENT_SELECT} FROM leads l
            LEFT JOIN funding_events e ON e.id=l.current_event_id WHERE l.id=?""",
        (lead_id,),
    ).fetchone()


def set_lead_status(conn: sqlite3.Connection, lead_id: int, status: str,
                    note: str | None = None) -> None:
    """Move a lead through the triage workflow (surfaced/contacted/snoozed/dead...).
    `note` records the human's reason (e.g. [Bad lead] feedback for future scoring)."""
    conn.execute("UPDATE leads SET status = ?, status_note = COALESCE(?, status_note) "
                 "WHERE id = ?", (status, note, lead_id))
    conn.commit()


def mark_surfaced(conn: sqlite3.Connection, lead_ids: list[int]) -> None:
    """Mark leads whose individual proactive alerts were confirmed by Slack."""
    conn.executemany("UPDATE leads SET status='surfaced' WHERE id=? AND status='new'",
                     [(i,) for i in lead_ids])
    conn.commit()


# ---------------------------------------------------------------- contacts (Phase 2)

def save_contact(conn: sqlite3.Connection, lead_id: int, name: str, title: str,
                 email: str, phone: str, source_url: str, confidence: str,
                 official_domain: str = "",
                 field_evidence: dict[str, bool] | None = None) -> int:
    """Store a VERIFIED contact (finder.py's gate already ran). Returns contact id."""
    cur = conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,source_url,confidence,contact_status,
              official_domain,field_evidence_json)
           VALUES (?,?,?,?,?,?,?,'verified',?,?)""",
        (lead_id, name, title, email, phone, source_url, confidence,
         official_domain or None,
         json.dumps(field_evidence, sort_keys=True) if field_evidence else None))
    conn.commit()
    return int(cur.lastrowid)


def mark_contact_not_found(conn: sqlite3.Connection, lead_id: int) -> None:
    """The honest outcome when enrichment finds nothing verifiable."""
    conn.execute("INSERT INTO contacts (lead_id, contact_status) VALUES (?, 'not_found')",
                 (lead_id,))
    conn.commit()


def contacts_for_lead(conn: sqlite3.Connection, lead_id: int) -> list[sqlite3.Row]:
    """All contact rows for a lead, verified first."""
    return list(conn.execute(
        "SELECT * FROM contacts WHERE lead_id = ? "
        "ORDER BY CASE contact_status WHEN 'verified' THEN 0 ELSE 1 END, id",
        (lead_id,)))


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
                channel: str, ts: str, style: str,
                delivery_key: str = "", event_id: int | None = None,
                urgent: bool = False) -> int:
    """Log a proactive Grant post (the thread anchor engagement attaches to)."""
    cur = conn.execute(
        """INSERT INTO posts
             (kind,lead_id,channel,ts,style,posted_at,delivery_key,event_id,urgent)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (kind, lead_id, channel, ts, style, _now(), delivery_key or None,
         event_id, int(urgent)))
    conn.commit()
    return int(cur.lastrowid)


def reserve_notification(conn: sqlite3.Connection, lead_id: int, event_id: int | None,
                         channel: str, delivery_class: str,
                         payload: dict[str, object]) -> str | None:
    """Atomically reserve one event/channel delivery before calling Slack.

    A stale ``sending`` state is intentionally not retried automatically: a network
    timeout can mean Slack accepted the post, so blind retrying could duplicate it.
    """
    delivery_key = f"{channel}:lead:{lead_id}:event:{event_id or 'projection'}"
    now = _now()
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO notification_outbox
                 (delivery_key,event_id,lead_id,audience,delivery_class,payload_json,
                  state,attempts,available_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'sending',1,?,?,?)""",
            (delivery_key, event_id, lead_id, channel, delivery_class,
             json.dumps(payload, sort_keys=True), now, now, now),
        )
    return delivery_key if cur.rowcount == 1 else None


def finish_notification(conn: sqlite3.Connection, delivery_key: str, state: str,
                        slack_ts: str = "", error: str = "") -> None:
    """Finalize a reserved Slack delivery as delivered or unknown."""
    if state not in {"delivered", "unknown"}:
        raise ValueError(f"unsupported notification state '{state}'")
    with conn:
        conn.execute(
            """UPDATE notification_outbox
               SET state=?,slack_ts=?,last_error=?,updated_at=? WHERE delivery_key=?""",
            (state, slack_ts or None, error or None, _now(), delivery_key),
        )


def find_post_by_ts(conn: sqlite3.Connection, channel: str, ts: str) -> sqlite3.Row | None:
    """Look up a Grant post from a thread anchor ts (to attribute engagement)."""
    return conn.execute("SELECT * FROM posts WHERE channel = ? AND ts = ?",
                        (channel, ts)).fetchone()


def claim_slack_event(conn: sqlite3.Connection, event_id: str, workspace: str,
                      channel: str, thread_ts: str, slack_user: str) -> bool:
    """Persistently claim one Slack delivery so restarts cannot process it twice."""
    if not event_id:
        return False
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO slack_event_receipts
                 (event_id,workspace,channel,thread_ts,slack_user,state,received_at)
               VALUES (?,?,?,?,?,'processing',?)""",
            (event_id, workspace, channel, thread_ts or None, slack_user or None, _now()),
        )
    return cur.rowcount == 1


def finish_slack_event(conn: sqlite3.Connection, event_id: str,
                       error: str = "", action_state: str = "complete",
                       delivery_state: str = "delivered") -> None:
    """Persist separate action and final-message outcomes for reconciliation."""
    allowed_actions = {"complete", "unknown"}
    allowed_deliveries = {"delivered", "failed", "unknown"}
    if action_state not in allowed_actions or delivery_state not in allowed_deliveries:
        raise ValueError("unsupported Slack receipt outcome")
    state = ("complete" if action_state == "complete" and delivery_state == "delivered"
             else "needs_reconciliation")
    with conn:
        conn.execute(
            """UPDATE slack_event_receipts
               SET state=?,action_state=?,delivery_state=?,finished_at=?,error=?
               WHERE event_id=?""",
            (state, action_state, delivery_state, _now(), error or None, event_id),
        )


def unresolved_slack_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return failed/unknown Slack turns awaiting explicit human reconciliation."""
    return list(conn.execute(
        """SELECT event_id,workspace,channel,thread_ts,slack_user,state,
                  action_state,delivery_state,finished_at,error
             FROM slack_event_receipts
            WHERE state='needs_reconciliation' AND reviewed_at IS NULL
            ORDER BY received_at"""))


def mark_slack_event_reviewed(conn: sqlite3.Connection, event_id: str) -> bool:
    """Acknowledge manual reconciliation without replaying any external action."""
    with conn:
        cur = conn.execute(
            """UPDATE slack_event_receipts SET reviewed_at=?
               WHERE event_id=? AND state='needs_reconciliation' AND reviewed_at IS NULL""",
            (_now(), event_id),
        )
    return cur.rowcount == 1


def record_engagement(conn: sqlite3.Connection, post_id: int, slack_user: str,
                      kind: str) -> bool:
    """+1 point when a human interacts with a post. Deduped per (post, user, kind)
    so one enthusiastic user can't inflate the score. Returns True if new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO engagement (post_id, slack_user, kind, at) "
        "VALUES (?,?,?,?)", (post_id, slack_user, kind, _now()))
    conn.commit()
    if cur.rowcount == 1:
        post = conn.execute("SELECT lead_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if post is not None:
            record_outcome(
                conn, int(post["lead_id"]) if post["lead_id"] is not None else None,
                post_id, slack_user, kind,
                f"engagement:{post_id}:{slack_user}:{kind}")
    return cur.rowcount == 1


_OUTCOME_POINTS = {
    "reaction": 1,
    "reply": 2,
    "question": 2,
    "claim": 4,
    "snoozed": -2,
    "bad_lead": -8,
    "contacted": 6,
    "campaign_added": 8,
}


def record_outcome(conn: sqlite3.Connection, lead_id: int | None,
                   post_id: int | None, slack_user: str, kind: str,
                   source_key: str) -> bool:
    """Persist one deduplicated human reward signal with an explicit point weight."""
    if kind not in _OUTCOME_POINTS:
        raise ValueError(f"unsupported outcome kind '{kind}'")
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO outcome_events
                 (id,lead_id,post_id,slack_user,kind,points,source_key,occurred_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), lead_id, post_id, slack_user, kind,
             _OUTCOME_POINTS[kind], source_key, _now()),
        )
    return cur.rowcount == 1


def program_outcome_points(conn: sqlite3.Connection, program: str) -> list[int]:
    """Return verified human outcome weights for one exact program label."""
    return [int(row[0]) for row in conn.execute(
        """SELECT o.points FROM outcome_events o
           JOIN leads l ON l.id=o.lead_id
           WHERE UPPER(COALESCE(l.program,''))=UPPER(?)""",
        (program or "",),
    )]


def engagement_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Grant's score: total points + per-kind breakdown (the tuning signal)."""
    stats = {"total": conn.execute("SELECT COUNT(*) FROM engagement").fetchone()[0]}
    for kind, n in conn.execute(
            "SELECT kind, COUNT(*) FROM engagement GROUP BY kind"):
        stats[kind] = n
    return stats


def posts_today(conn: sqlite3.Connection, channel: str,
                now_utc: datetime | None = None) -> list[sqlite3.Row]:
    """Today's proactive posts in Pacific time, where the Slack team operates."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local_date = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).date()
    start_local = datetime.combine(
        local_date, time.min, tzinfo=ZoneInfo("America/Los_Angeles"))
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    return list(conn.execute(
        """SELECT * FROM posts WHERE channel=? AND posted_at>=? AND posted_at<?
           ORDER BY posted_at,id""",
        (channel, start_utc.isoformat(), end_utc.isoformat())))


def nugget_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unsurfaced GOLD leads eligible for a drip nugget."""
    return list(conn.execute(
        f"""SELECT {_LEAD_EVENT_SELECT}, {_CRM_CONTEXT_SELECT}
            FROM leads l
            JOIN funding_events e ON e.id=l.current_event_id
            WHERE l.lead_grade='gold' AND l.status='new' AND e.suppressed=0
              AND e.verification_status='verified'
              AND e.event_type IN ('award_announced','award_obligated')"""))


def bulletin_candidates(conn: sqlite3.Connection, max_age_days: int = 14
                        ) -> list[sqlite3.Row]:
    """Return fresh federal or California application-window bulletins.

    These are program-level signals rather than award evidence. The earliest
    verified closing date sorts first so users see the most time-sensitive item.
    """
    return list(conn.execute(
        f"""SELECT {_LEAD_EVENT_SELECT} FROM leads l
            JOIN funding_events e ON e.id=l.current_event_id
            WHERE l.source IN ('grants.gov','ca-grants-portal')
              AND l.first_seen >= datetime('now', ?)
              AND e.suppressed=0 AND e.verification_status='verified'
              AND e.event_type='application_window_opened'
              AND l.id NOT IN (SELECT lead_id FROM posts WHERE lead_id IS NOT NULL)
              AND l.funds_end != '' AND date(l.funds_end) >= date('now')
            ORDER BY date(l.funds_end) ASC,l.id""", (f"-{max_age_days} days",)))
