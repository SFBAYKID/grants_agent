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
from pathlib import Path

from .db_common import (
    CRM_CONTEXT_SELECT as _CRM_CONTEXT_SELECT,
    LEAD_EVENT_SELECT as _LEAD_EVENT_SELECT,
    _now,
)
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


# Sources whose detail_url addresses exactly ONE record, so the URL can be trusted as a
# second identity when the item_id formula changes shape (see _adopt_drifted_lead).
# Deliberately an allowlist, never blanket: most sources point many items at one program
# landing page, where matching on URL would merge genuinely distinct leads.
_PER_RECORD_URL_SOURCES: frozenset[str] = frozenset({"rfp"})


def _adopt_drifted_lead(
    conn: sqlite3.Connection,
    source: str,
    url: str,
    entity_key: str,
    new_item_id: str,
) -> sqlite3.Row | None:
    """Adopt a stored lead that IS this item but was keyed under an older item_id format,
    re-keying it in place. Returns None when there is no such row.

    Why: upsert_lead identifies a lead only by (source, source_item_id), so changing an
    item_id formula orphans every row already stored under the old shape and the next
    poll re-inserts each one as a brand-new lead — a duplicate alert to the channel. This
    happened for real: eabf6e5 switched rfp_item_id from a 6-token title prefix to the
    full title, and the same Pennsylvania DOC solicitation landed twice. For sources whose
    detail_url identifies one record, the URL survives that change, so the old row is
    adopted instead of duplicated. Re-keying IN PLACE keeps the lead id stable, so posts,
    receipts, and CRM links that already reference it stay valid.

    The ORGANIZATION must match too, not just the URL. A URL alone is too weak an identity
    — distinct buyers can share a portal or landing URL, and fusing two agencies into one
    lead would silently destroy a real lead (Constitution rule 1). Same source + same URL +
    same organization is the narrow case that actually means "this row was re-keyed".
    """
    if source not in _PER_RECORD_URL_SOURCES or not url or not entity_key:
        return None
    row = conn.execute(
        """SELECT * FROM leads
           WHERE source=? AND detail_url=? AND canonical_entity_key=?
           ORDER BY id LIMIT 1""",
        (source, url, entity_key),
    ).fetchone()
    if row is None:
        return None
    # Cannot violate UNIQUE(source, source_item_id): the caller only reaches here when no
    # row holds new_item_id. Oldest row wins, so if a pair already duplicated, the item
    # collapses back onto the original — the one carrying the post history.
    conn.execute(
        "UPDATE leads SET source_item_id=? WHERE id=?", (new_item_id, int(row["id"]))
    )
    return conn.execute(
        "SELECT * FROM leads WHERE id=?", (int(row["id"]),)
    ).fetchone()


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
    adopted = False
    if existing is None:
        # No row under this key: it may still be a lead we already hold under a previous
        # item_id format. Adopting it prevents a re-keyed item from alerting twice.
        existing = _adopt_drifted_lead(
            conn,
            it.source,
            it.url,
            canonical_entity_key(it.entity, it.state),
            str(it.item_id),
        )
        adopted = existing is not None
    inserted = existing is None
    if inserted:
        conn.execute(
            """INSERT INTO leads (source, source_item_id, lead_grade, entity_name,
                                  title, entity_type, state, program, amount,
                                  funds_start, funds_end, detail_url, raw_json,
                                  first_seen, last_seen, canonical_entity_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                it.source,
                str(it.item_id),
                lead.grade.value,
                it.entity,
                it.title,
                lead.entity_type,
                it.state,
                it.program,
                it.amount,
                it.start or None,
                it.end or None,
                it.url,
                raw_json,
                now,
                now,
                canonical_entity_key(it.entity, it.state),
            ),
        )
        existing = conn.execute(
            "SELECT * FROM leads WHERE source=? AND source_item_id=?",
            (it.source, str(it.item_id)),
        ).fetchone()
    assert existing is not None  # inserted above or selected before the transaction

    incoming = (
        lead.grade.value,
        it.entity,
        it.title,
        lead.entity_type,
        it.state,
        it.program,
        it.amount,
        it.start or None,
        it.end or None,
        it.url,
        raw_json,
        canonical_entity_key(it.entity, it.state),
    )
    suppressed = it.backfill or it.event_type == FundingEventType.RECORD_OBSERVED

    lead_id = int(existing["id"])
    observation = None
    if adopted:
        # The lead was just re-keyed, so an observation of this EXACT payload already
        # exists under the old item_id. source_observations is keyed by
        # (source, source_item_id, payload_hash), so inserting again would mint a second
        # observation and therefore a second funding_event — the same duplicate alert,
        # one level down. Reuse the prior observation instead. Past observations are
        # never rewritten: what was observed, under the key it was observed with, stands.
        observation = conn.execute(
            """SELECT id FROM source_observations
               WHERE lead_id=? AND source=? AND payload_hash=? ORDER BY id LIMIT 1""",
            (lead_id, it.source, payload_hash),
        ).fetchone()
    if observation is None:
        conn.execute(
            """INSERT OR IGNORE INTO source_observations
                 (lead_id, source, source_item_id, observed_at, payload_hash, raw_json,
                  source_url, source_locator, verification_status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                lead_id,
                it.source,
                str(it.item_id),
                now,
                payload_hash,
                raw_json,
                it.url,
                it.source_locator,
                it.verification_status.value,
            ),
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
        (
            lead_id,
            int(observation["id"]),
            it.event_type.value,
            it.event_date or None,
            it.date_precision.value,
            it.amount,
            it.funded_scope,
            it.eligible_scope,
            it.application_portal,
            it.evidence_excerpt or it.title,
            payload_hash,
            it.url,
            it.source_locator,
            it.verification_status.value,
            int(it.backfill),
            int(suppressed),
            now,
        ),
    )
    event_created = event_insert.rowcount == 1
    if event_created:
        conn.execute(
            "UPDATE leads SET current_event_id=? WHERE id=?",
            (int(event_insert.lastrowid), lead_id),
        )
    if not inserted:
        conn.execute(
            """UPDATE leads SET lead_grade=?, entity_name=?, title=?, entity_type=?,
                      state=?, program=?, amount=?, funds_start=?, funds_end=?,
                      detail_url=?, raw_json=?, last_seen=?, canonical_entity_key=?,
                      status=CASE WHEN ? AND ?=0 THEN 'new' ELSE status END
                 WHERE id=?""",
            (
                *incoming[:10],
                incoming[10],
                now,
                incoming[11],
                int(event_created),
                int(suppressed),
                lead_id,
            ),
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
        (
            started,
            _now(),
            stats.source,
            stats.items_seen,
            stats.items_new,
            stats.errors,
            int(stats.complete),
            stats.error_code,
        ),
    )
    conn.commit()


def acquire_poll_lock(
    conn: sqlite3.Connection, name: str, owner: str, stale_hours: int = 2
) -> bool:
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
            inserted = upsert_lead(
                conn,
                Lead(
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
                ),
            )
            if inserted:
                new += 1
    return rows, new


def status_summary(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(source, grade, count) rows for the CLI status command."""
    return list(
        conn.execute(
            "SELECT source, lead_grade, COUNT(*) FROM leads GROUP BY source, lead_grade "
            "ORDER BY source, lead_grade"
        )
    )


def save_search_request(
    conn: sqlite3.Connection,
    session_key: str,
    requested_by: str,
    filters: dict[str, object],
    scope: str,
    top_n: int | None,
    format_name: str,
    lead_ids: list[int],
) -> str:
    """Persist one immutable completed search snapshot for follow-up CRM/export actions."""
    request_id = str(uuid.uuid4())
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO search_requests
                 (id,session_key,requested_by,filters_json,scope,top_n,format,state,
                  result_lead_ids_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'complete',?,?,?)""",
            (
                request_id,
                session_key,
                requested_by,
                json.dumps(filters, sort_keys=True, default=str),
                scope,
                top_n,
                format_name or None,
                json.dumps(lead_ids),
                now,
                now,
            ),
        )
    return request_id


def get_search_request(
    conn: sqlite3.Connection, request_id: str, requested_by: str
) -> sqlite3.Row | None:
    """Return a completed search snapshot only to its initiating Slack user."""
    return conn.execute(
        "SELECT * FROM search_requests WHERE id=? AND requested_by=? AND state='complete'",
        (request_id, requested_by),
    ).fetchone()


def create_export_job(
    conn: sqlite3.Connection,
    requested_by: str,
    format_name: str,
    idempotency_key: str,
    search_request_id: str | None = None,
) -> str:
    """Persist an export attempt before artifact creation or external API calls."""
    job_id = str(uuid.uuid4())
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO export_jobs
                 (id,search_request_id,requested_by,format,idempotency_key,state,
                  created_at,updated_at)
               VALUES (?,?,?,?,?,'creating',?,?)""",
            (
                job_id,
                search_request_id,
                requested_by,
                format_name,
                idempotency_key,
                now,
                now,
            ),
        )
    return job_id


def finish_export_job(
    conn: sqlite3.Connection,
    job_id: str,
    state: str,
    url: str = "",
    external_id: str = "",
    error: str = "",
) -> None:
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
        f"""SELECT {_LEAD_EVENT_SELECT}, {_CRM_CONTEXT_SELECT} FROM leads l
            LEFT JOIN funding_events e ON e.id=l.current_event_id WHERE l.id=?""",
        (lead_id,),
    ).fetchone()


def set_lead_status(
    conn: sqlite3.Connection, lead_id: int, status: str, note: str | None = None
) -> None:
    """Move a lead through the triage workflow (surfaced/contacted/snoozed/dead...).
    `note` records the human's reason (e.g. [Bad lead] feedback for future scoring)."""
    conn.execute(
        "UPDATE leads SET status = ?, status_note = COALESCE(?, status_note) "
        "WHERE id = ?",
        (status, note, lead_id),
    )
    conn.commit()


def mark_surfaced(conn: sqlite3.Connection, lead_ids: list[int]) -> None:
    """Mark leads whose individual proactive alerts were confirmed by Slack."""
    conn.executemany(
        "UPDATE leads SET status='surfaced' WHERE id=? AND status='new'",
        [(i,) for i in lead_ids],
    )
    conn.commit()


# ---------------------------------------------------------------- contacts (Phase 2)


def save_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str,
    title: str,
    email: str,
    phone: str,
    source_url: str,
    confidence: str,
    official_domain: str = "",
    field_evidence: dict[str, bool] | None = None,
) -> int:
    """Store a VERIFIED contact (finder.py's gate already ran). Returns contact id."""
    cur = conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,source_url,confidence,contact_status,
              official_domain,field_evidence_json)
           VALUES (?,?,?,?,?,?,?,'verified',?,?)""",
        (
            lead_id,
            name,
            title,
            email,
            phone,
            source_url,
            confidence,
            official_domain or None,
            json.dumps(field_evidence, sort_keys=True) if field_evidence else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_linkedin_contact(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str,
    title: str,
    profile_url: str,
) -> int:
    """Store a LinkedIn-sourced person: name/title/profile only, never an email.

    Distinct from save_contact because the evidence class differs — a profile's
    ownership is not verified, so contact_status is 'linkedin_only'."""
    cur = conn.execute(
        """INSERT INTO contacts
             (lead_id,name,title,email,phone,source_url,confidence,contact_status)
           VALUES (?,?,?,NULL,NULL,?,'medium','linkedin_only')""",
        (lead_id, name, title, profile_url),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_contact_not_found(conn: sqlite3.Connection, lead_id: int) -> None:
    """The honest outcome when enrichment finds nothing verifiable."""
    conn.execute(
        "INSERT INTO contacts (lead_id, contact_status) VALUES (?, 'not_found')",
        (lead_id,),
    )
    conn.commit()


def save_org_profile(conn: sqlite3.Connection, lead_id: int, profile: object) -> None:
    """Persist verbatim-verified organization details onto the lead.

    ``profile`` is an OrgProfile (duck-typed to avoid an enrich import here). Only
    values that already passed on-page verification reach this function."""
    conn.execute(
        """UPDATE leads SET org_website=?, org_general_email=?, org_phone=?,
             org_street=?, org_city=?, org_state=?, org_postal_code=?,
             org_profile_status=?, org_profile_source_url=? WHERE id=?""",
        (
            getattr(profile, "website", "") or None,
            getattr(profile, "general_email", "") or None,
            getattr(profile, "phone", "") or None,
            getattr(profile, "street", "") or None,
            getattr(profile, "city", "") or None,
            getattr(profile, "state", "") or None,
            getattr(profile, "postal_code", "") or None,
            getattr(profile, "status", "not_found"),
            getattr(profile, "source_url", "") or None,
            lead_id,
        ),
    )
    conn.commit()


def contacts_for_lead(conn: sqlite3.Connection, lead_id: int) -> list[sqlite3.Row]:
    """All contact rows for a lead, verified first."""
    return list(
        conn.execute(
            "SELECT * FROM contacts WHERE lead_id = ? "
            "ORDER BY CASE contact_status WHEN 'verified' THEN 0 ELSE 1 END, id",
            (lead_id,),
        )
    )


# ---------------------------------------------------------------- drip engine + conversation state


def record_post(
    conn: sqlite3.Connection,
    kind: str,
    lead_id: int | None,
    channel: str,
    ts: str,
    style: str,
    delivery_key: str = "",
    event_id: int | None = None,
    urgent: bool = False,
) -> int:
    """Log a proactive Grant post (the thread anchor engagement attaches to)."""
    cur = conn.execute(
        """INSERT INTO posts
             (kind,lead_id,channel,ts,style,posted_at,delivery_key,event_id,urgent)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            kind,
            lead_id,
            channel,
            ts,
            style,
            _now(),
            delivery_key or None,
            event_id,
            int(urgent),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def find_post_by_ts(
    conn: sqlite3.Connection, channel: str, ts: str
) -> sqlite3.Row | None:
    """Look up a Grant post from a thread anchor ts (to attribute engagement)."""
    return conn.execute(
        "SELECT * FROM posts WHERE channel = ? AND ts = ?", (channel, ts)
    ).fetchone()


def register_conversation_thread(
    conn: sqlite3.Connection,
    workspace: str,
    channel: str,
    thread_ts: str,
    initiated_by: str,
) -> None:
    """Persist a configured-channel thread that began with an explicit @Grant mention."""
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO slack_conversation_threads
                 (workspace,channel,thread_ts,initiated_by,created_at,last_active_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(workspace,channel,thread_ts) DO UPDATE SET
                 last_active_at=excluded.last_active_at""",
            (workspace, channel, thread_ts, initiated_by, now, now),
        )


def is_conversation_thread(
    conn: sqlite3.Connection, workspace: str, channel: str, thread_ts: str
) -> bool:
    """Return whether plain replies may continue a prior @Grant conversation."""
    row = conn.execute(
        """SELECT 1 FROM slack_conversation_threads
           WHERE workspace=? AND channel=? AND thread_ts=?""",
        (workspace, channel, thread_ts),
    ).fetchone()
    return row is not None


def touch_conversation_thread(
    conn: sqlite3.Connection, workspace: str, channel: str, thread_ts: str
) -> None:
    """Record activity after a routed plain reply without widening thread access."""
    with conn:
        conn.execute(
            """UPDATE slack_conversation_threads SET last_active_at=?
               WHERE workspace=? AND channel=? AND thread_ts=?""",
            (_now(), workspace, channel, thread_ts),
        )


def claim_slack_event(
    conn: sqlite3.Connection,
    event_id: str,
    workspace: str,
    channel: str,
    thread_ts: str,
    slack_user: str,
) -> bool:
    """Persistently claim one Slack delivery so restarts cannot process it twice."""
    if not event_id:
        return False
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO slack_event_receipts
                 (event_id,workspace,channel,thread_ts,slack_user,state,received_at)
               VALUES (?,?,?,?,?,'processing',?)""",
            (
                event_id,
                workspace,
                channel,
                thread_ts or None,
                slack_user or None,
                _now(),
            ),
        )
    return cur.rowcount == 1


def finish_slack_event(
    conn: sqlite3.Connection,
    event_id: str,
    error: str = "",
    action_state: str = "complete",
    delivery_state: str = "delivered",
) -> None:
    """Persist separate action and final-message outcomes for reconciliation."""
    allowed_actions = {"complete", "unknown"}
    allowed_deliveries = {"delivered", "failed", "unknown"}
    if action_state not in allowed_actions or delivery_state not in allowed_deliveries:
        raise ValueError("unsupported Slack receipt outcome")
    state = (
        "complete"
        if action_state == "complete" and delivery_state == "delivered"
        else "needs_reconciliation"
    )
    with conn:
        conn.execute(
            """UPDATE slack_event_receipts
               SET state=?,action_state=?,delivery_state=?,finished_at=?,error=?
               WHERE event_id=?""",
            (state, action_state, delivery_state, _now(), error or None, event_id),
        )


def unresolved_slack_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return failed/unknown Slack turns awaiting explicit human reconciliation."""
    return list(
        conn.execute(
            """SELECT event_id,workspace,channel,thread_ts,slack_user,state,
                  action_state,delivery_state,finished_at,error
             FROM slack_event_receipts
            WHERE state='needs_reconciliation' AND reviewed_at IS NULL
            ORDER BY received_at"""
        )
    )


def mark_slack_event_reviewed(conn: sqlite3.Connection, event_id: str) -> bool:
    """Acknowledge manual reconciliation without replaying any external action."""
    with conn:
        cur = conn.execute(
            """UPDATE slack_event_receipts SET reviewed_at=?
               WHERE event_id=? AND state='needs_reconciliation' AND reviewed_at IS NULL""",
            (_now(), event_id),
        )
    return cur.rowcount == 1


# Human-signal and drip-selection queries live in db_engagement.py (file-size cap).
# Re-exported here so `db.<name>` stays the single persistence entry point for callers.
from .db_delivery import (  # noqa: E402  (facade re-export, must follow definitions)
    blocked_notifications,
    channel_guard,
    channel_guard_any,
    clear_channel_guard,
    finish_notification,
    quarantine_lead,
    release_notification,
    reserve_notification,
    set_channel_guard,
)
from .db_engagement import (  # noqa: E402  (facade re-export, must follow definitions)
    bulletin_candidates,
    delivery_attempts_today,
    engagement_stats,
    nugget_candidates,
    posts_today,
    program_outcome_points,
    recent_post_states,
    record_engagement,
    record_outcome,
    rfp_candidates,
)

__all__ = [
    "blocked_notifications",
    "bulletin_candidates",
    "channel_guard",
    "channel_guard_any",
    "clear_channel_guard",
    "finish_notification",
    "quarantine_lead",
    "release_notification",
    "reserve_notification",
    "set_channel_guard",
    "delivery_attempts_today",
    "engagement_stats",
    "nugget_candidates",
    "posts_today",
    "program_outcome_points",
    "recent_post_states",
    "record_engagement",
    "record_outcome",
    "rfp_candidates",
]
