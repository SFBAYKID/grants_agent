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

from . import poll_lease
from .db_contacts import (  # re-export: every db.<name> call site is unchanged
    contact_is_page_verified,  # noqa: F401
    mark_contact_not_found,  # noqa: F401
    save_contact,  # noqa: F401
    save_human_asserted_contact,  # noqa: F401
    save_linkedin_contact,  # noqa: F401
    save_vendor_contact,  # noqa: F401
)
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
# Interactive search may show every lead except an explicitly dead one so a rep can
# inspect historical/snoozed context. Mutating Campaign actions are narrower: a human
# disposition of snoozed or not_relevant is an instruction, not just display metadata.
SEARCHABLE_LEAD_PREDICATE = "COALESCE(status, 'new') != 'dead'"
CAMPAIGN_ELIGIBLE_STATUSES: frozenset[str] = frozenset({"new", "surfaced", "contacted"})
CAMPAIGN_ELIGIBLE_LEAD_PREDICATE = (
    "COALESCE(status, 'new') IN ('new','surfaced','contacted')"
)


def campaign_status_eligible(status: object) -> bool:
    """Return whether one lead disposition permits a Campaign mutation."""
    return str(status or "new") in CAMPAIGN_ELIGIBLE_STATUSES


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
    return conn.execute("SELECT * FROM leads WHERE id=?", (int(row["id"]),)).fetchone()


def _upsert_lead(conn: sqlite3.Connection, lead: Lead) -> bool:
    """Project one source item inside the caller's write transaction.

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
    # Projection-only metadata changes deliberately do not drive notifications.
    return inserted or (event_created and not suppressed)


def upsert_lead(
    conn: sqlite3.Connection,
    lead: Lead,
    *,
    lease: poll_lease.PollLease | None = None,
) -> bool:
    """Project one source item, optionally fenced to a current polling lease.

    The lease check and every projection/event write share one ``BEGIN IMMEDIATE``
    transaction. A paused worker whose token was replaced therefore cannot resume
    and commit stale source data after its successor starts.
    """
    with poll_lease.fenced_transaction(conn, lease):
        return _upsert_lead(conn, lead)


def begin_run(
    conn: sqlite3.Connection,
    source: str,
    started: str,
    *,
    lease: poll_lease.PollLease | None = None,
) -> int:
    """Open a run row in state 'pending' BEFORE processing and return its id.

    The rich-card freshness rule (Chase A1) advances a lead's confirmation only after
    the run that re-confirmed it is durably marked complete AND successful. That requires
    the run to have an identity DURING processing, so it is created up front here and
    resolved by `complete_run`/`fail_run`. A failed/partial/interrupted/dry run never
    reaches `complete_run`, so it can never advance freshness."""
    with poll_lease.fenced_transaction(conn, lease):
        cur = conn.execute(
            """INSERT INTO runs
                 (started, source, state, items_seen, items_new, errors, complete,
                  error_code)
               VALUES (?,?, 'pending', 0, 0, '', 0, '')""",
            (started, source),
        )
    return int(cur.lastrowid)


def complete_run(
    conn: sqlite3.Connection,
    run_id: int,
    stats: RunStats,
    confirmed_keys: list[tuple[str, str]],
    *,
    lease: poll_lease.PollLease | None = None,
) -> None:
    """Mark a run complete AND advance confirmation freshness for exactly the leads it
    re-confirmed — in ONE transaction. Call ONLY when `stats.complete` is true.

    `confirmed_keys` is every `(source, source_item_id)` the successful run saw (new AND
    unchanged), because an unchanged item that is still present in a completed run is
    precisely what "still fresh" means. `last_confirmed_at` is the completion time, never
    `observed_at` (first-sighting) or `last_seen`."""
    now = _now()
    with poll_lease.fenced_transaction(conn, lease):
        conn.execute(
            """UPDATE runs SET finished=?, state='complete', items_seen=?, items_new=?,
                     errors=?, complete=1, error_code=? WHERE id=?""",
            (
                now,
                stats.items_seen,
                stats.items_new,
                stats.errors,
                stats.error_code,
                run_id,
            ),
        )
        if confirmed_keys:
            conn.executemany(
                """UPDATE leads SET last_confirmed_run_id=?, last_confirmed_at=?
                   WHERE source=? AND source_item_id=?""",
                [(run_id, now, src, iid) for src, iid in confirmed_keys],
            )


def fail_run(
    conn: sqlite3.Connection,
    run_id: int,
    stats: RunStats,
    *,
    lease: poll_lease.PollLease | None = None,
) -> None:
    """Mark a run failed/partial. NEVER advances confirmation freshness (Chase A1)."""
    with poll_lease.fenced_transaction(conn, lease):
        conn.execute(
            """UPDATE runs SET finished=?, state='failed', items_seen=?, items_new=?,
                     errors=?, complete=0, error_code=? WHERE id=?""",
            (
                _now(),
                stats.items_seen,
                stats.items_new,
                stats.errors,
                stats.error_code,
                run_id,
            ),
        )


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
    total_count: int,
    result_complete: bool,
) -> str:
    """Persist one immutable search snapshot with explicit completeness evidence."""
    request_id = str(uuid.uuid4())
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO search_requests
                 (id,session_key,requested_by,filters_json,scope,top_n,format,state,
                  result_lead_ids_json,total_count,result_complete,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'complete',?,?,?,?,?)""",
            (
                request_id,
                session_key,
                requested_by,
                json.dumps(filters, sort_keys=True, default=str),
                scope,
                top_n,
                format_name or None,
                json.dumps(lead_ids),
                total_count,
                int(result_complete),
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


def reconcile_seed_duplicates(
    conn: sqlite3.Connection, *, lease: poll_lease.PollLease | None = None
) -> int:
    """Retire seed-CSV rows that a live poller row has superseded.

    Why: the 2026-07-13 live output showed the same award twice — once from
    'seed:svpp_csv' (no award id, no URL) and once from live USASpending. Match is
    EXACT on normalized entity + amount + funds_end (verified 75/75 seed rows matched
    this way with zero false lonelies). The live row wins (it carries the award id and
    deep link); the seed row goes to status='dead' with an explanatory note, preserving
    history. Returns how many seed rows were retired. Idempotent.
    """
    with poll_lease.fenced_transaction(conn, lease):
        cur = conn.execute("""
            UPDATE leads SET status = 'dead',
                   status_note =
                     'superseded by live award row (same entity/amount/window)'
            WHERE source = 'seed:svpp_csv' AND status != 'dead' AND EXISTS (
                SELECT 1 FROM leads l
                WHERE l.source LIKE 'usaspending:%'
                  AND UPPER(TRIM(l.entity_name)) = UPPER(TRIM(leads.entity_name))
                  AND l.amount = leads.amount
                  AND l.funds_end = leads.funds_end)""")
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


_ORG_EVIDENCE_FIELDS = frozenset(
    {"website", "general_email", "phone", "street", "city", "state", "postal_code"}
)


def _accepted_org_evidence(profile: object) -> dict[str, object]:
    """Return only internally complete evidence that exactly binds a projection."""
    accepted: dict[str, object] = {}
    supplied = dict(getattr(profile, "field_evidence", {}) or {})
    for field_name in _ORG_EVIDENCE_FIELDS:
        value = str(getattr(profile, field_name, "") or "")
        match = supplied.get(field_name)
        if not value or match is None:
            continue
        if (
            str(getattr(match, "field", "")) == field_name
            and str(getattr(match, "value", "")) == value
            and str(getattr(match, "source_url", ""))
            and str(getattr(match, "excerpt", ""))
            and str(getattr(match, "evidence_hash", ""))
            and str(getattr(match, "verifier_version", ""))
        ):
            accepted[field_name] = match
    return accepted


def save_org_profile(conn: sqlite3.Connection, lead_id: int, profile: object) -> None:
    """Persist verbatim-verified organization details onto the lead.

    ``profile`` is an OrgProfile (duck-typed to avoid an enrich import here). A
    projection is accepted only with exact field evidence; a caller cannot bypass
    the verifier by constructing an unchecked profile object.
    """
    field_evidence = _accepted_org_evidence(profile)
    values = {
        field_name: str(getattr(profile, field_name, "") or "")
        if field_name in field_evidence
        else ""
        for field_name in _ORG_EVIDENCE_FIELDS
    }
    source_urls = {
        str(getattr(match, "source_url", "")) for match in field_evidence.values()
    }
    requested_source = str(getattr(profile, "source_url", "") or "")
    source_url = (
        requested_source
        if requested_source in source_urls
        else next(iter(sorted(source_urls)), "")
    )
    status = str(getattr(profile, "status", "not_found") or "not_found")
    if status == "found" and not field_evidence:
        status = "not_found"
    with conn:
        conn.execute(
            """UPDATE leads SET org_website=?, org_website_candidate=?,
             org_general_email=?, org_phone=?,
             org_street=?, org_city=?, org_state=?, org_postal_code=?,
            org_profile_status=?, org_profile_source_url=?,
            org_profile_checked_at=? WHERE id=?""",
            (
                values["website"] or None,
                getattr(profile, "website_candidate", "") or None,
                values["general_email"] or None,
                values["phone"] or None,
                values["street"] or None,
                values["city"] or None,
                values["state"] or None,
                values["postal_code"] or None,
                status,
                source_url or None,
                # Stamped on EVERY outcome, including not_found and unreachable. A
                # clock that only recorded successes would leave exactly the failures
                # the cooldown exists to space out looking like they never ran.
                _now(),
                lead_id,
            ),
        )
        conn.execute(
            """UPDATE organization_field_evidence SET status='superseded'
               WHERE lead_id=? AND status='current'""",
            (lead_id,),
        )
        for field_name, match in field_evidence.items():
            conn.execute(
                """INSERT INTO organization_field_evidence
                     (id,lead_id,field_name,field_value,source_url,excerpt,
                      evidence_hash,verifier_version,status,verified_at)
                   VALUES (?,?,?,?,?,?,?,?,'current',?)""",
                (
                    uuid.uuid4().hex,
                    lead_id,
                    field_name,
                    str(getattr(match, "value", "")),
                    str(getattr(match, "source_url", "")),
                    str(getattr(match, "excerpt", ""))[:500],
                    str(getattr(match, "evidence_hash", "")),
                    str(getattr(match, "verifier_version", "")),
                    _now(),
                ),
            )


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
    snapshot_id: str = "",
) -> int:
    """Log a proactive Grant post (the thread anchor engagement attaches to)."""
    cur = conn.execute(
        """INSERT INTO posts
             (kind,lead_id,channel,ts,style,posted_at,delivery_key,event_id,urgent,
              snapshot_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
            snapshot_id or None,
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
    rejections_today,
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
    "rejections_today",
    "program_outcome_points",
    "recent_post_states",
    "record_engagement",
    "record_outcome",
    "rfp_candidates",
]
