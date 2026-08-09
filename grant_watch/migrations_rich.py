"""Forward-only migrations for the rich award-card campaign, versions 14-26.

Kept separate so the ordered migration ledger remains below the repository's hard
1,000-line cap. Functions remain idempotent at their version boundary.

Migrations 14-22 moved here from `migrations.py` when that file crossed the cap. They
were always the rich-card era — the rich post kind, the snapshot tables, the card
actions, and the contact/activity/preparation evidence those snapshots freeze — so
they belong beside 23-26 rather than in the general ledger. The move is textual only:
same SQL, same order, same version numbers.
"""

from __future__ import annotations

import sqlite3

from .migration_runner import execute_script as _execute_script


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the existing columns for one known migration table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(conn: sqlite3.Connection, table: str, definition: str) -> None:
    """Add one column without disturbing databases that already contain it."""
    if definition.split()[0] not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migration_14_run_confirmation_freshness(conn: sqlite3.Connection) -> None:
    """Back the rich-card freshness rule with a COMPLETED-RUN confirmation signal.

    The honesty problem (architectural-critic C1 / Chase A1): observations are written
    once (`INSERT OR IGNORE`), so `source_observations.observed_at` is FIRST-sighting,
    never "the latest successful run re-confirmed this item still exists." Freshness must
    not fall back to `last_seen` either. So the LEAD PROJECTION (mutable, not the frozen
    observation) records when a COMPLETE, SUCCESSFUL run last re-confirmed the item.

    `runs.state` distinguishes pending/complete/failed (existing `complete` INT is set
    only at log time and has no 'pending'); historical runs default to 'complete'.
    `leads.last_confirmed_run_id/last_confirmed_at` are advanced by `cmd_poll` ONLY after
    a run is transactionally marked complete — never on a failed/partial/interrupted/
    dry run. Additive columns; old code ignores them (rollback-safe)."""
    _add_col(conn, "runs", "state TEXT DEFAULT 'complete'")
    _add_col(conn, "leads", "last_confirmed_run_id INTEGER")
    _add_col(conn, "leads", "last_confirmed_at TIMESTAMP")


def migration_15_rich_post_kind_and_snapshot_links(conn: sqlite3.Connection) -> None:
    """Admit the rich award card as a posts.kind, and link posts/outbox to a snapshot.

    The rich card MUST write a `posts` row (thread attribution runs through
    `find_post_by_ts` over `posts`), but `posts.kind` admits only the four drip kinds
    after v13 — a new kind would raise a CHECK violation AFTER `chat_postMessage` already
    landed (the migration-13 wedge, critic C3). SQLite can't ALTER a CHECK, so rebuild
    `posts` with the widened constraint, preserving every column, row, and id
    (engagement.post_id references stay valid; FK enforcement is off for the run). The
    rebuild also adds the nullable `snapshot_id` link. `notification_outbox` gets the same
    nullable link. Old code never selects `snapshot_id` (rollback-safe)."""
    _execute_script(
        conn,
        """
        CREATE TABLE posts_new (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL
            CHECK(kind IN ('platinum','nugget','rfp','bulletin','rich_award')),
          lead_id INTEGER REFERENCES leads(id),
          channel TEXT NOT NULL,
          ts TEXT NOT NULL,
          style TEXT,
          posted_at TIMESTAMP,
          event_id INTEGER,
          delivery_key TEXT,
          delivery_status TEXT,
          urgent INTEGER DEFAULT 0,
          snapshot_id TEXT,
          UNIQUE(channel, ts)
        );
        INSERT INTO posts_new
          (id,kind,lead_id,channel,ts,style,posted_at,event_id,delivery_key,
           delivery_status,urgent)
          SELECT id,kind,lead_id,channel,ts,style,posted_at,event_id,delivery_key,
                 delivery_status,urgent
          FROM posts;
        DROP TABLE posts;
        ALTER TABLE posts_new RENAME TO posts;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_delivery_key
          ON posts(delivery_key) WHERE delivery_key IS NOT NULL;
        """,
    )
    _add_col(conn, "notification_outbox", "snapshot_id TEXT")


def migration_16_rich_card_snapshots(conn: sqlite3.Connection) -> None:
    """The IMMUTABLE frozen rich card. Everything a thread/button/outcome/Persequor
    request needs is copied here at prepare time; nothing reads the mutable
    `leads.current_event_id` after freeze. Evidence references (`event_id`,
    `observation_id`, `run_id`) are PLAIN integers with NO enforced FK, so the known
    delete-based data-reconciliation procedure (2026-07-21 deleted funding_events rows)
    is never wedged by a frozen card (critic H5).

    Uniqueness prevents duplicate delivery of the same real award to the same audience,
    keyed on a STABLE identity (not the `event_id` surrogate, which drifts on re-key —
    the rfp_item_id incident, critic C2): `dedup_key` = canonical_entity_key + program +
    stable award id + audience. `policy_version` is provenance ONLY, never in the key."""
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS rich_card_snapshots (
          id TEXT PRIMARY KEY,
          policy_version INTEGER NOT NULL,
          audience TEXT NOT NULL,
          dedup_key TEXT NOT NULL,
          lead_id INTEGER,
          event_id INTEGER,
          observation_id INTEGER,
          run_id INTEGER,
          tier TEXT NOT NULL CHECK(tier IN ('gold','platinum')),
          entity_name TEXT NOT NULL,
          entity_kind TEXT NOT NULL
            CHECK(entity_kind IN ('city','school','school_district')),
          entity_kind_provenance TEXT NOT NULL
            CHECK(entity_kind_provenance IN ('source','nces','census','reviewed')),
          state TEXT,
          state_provenance TEXT,
          program TEXT,
          amount REAL,
          award_date TEXT,
          award_date_precision TEXT,
          spend_window_start TEXT,
          spend_window_end TEXT,
          award_url TEXT,
          official_website TEXT,
          contact_name TEXT,
          contact_type TEXT
            CHECK(contact_type IS NULL OR contact_type IN ('named_direct','official_general')),
          contact_email TEXT,
          contact_evidence_url TEXT,
          contact_verified_at TIMESTAMP,
          contact_expires_at TIMESTAMP,
          sf_lookup_status TEXT,
          sf_account_id TEXT,
          sf_open_opp_id TEXT,
          sf_activity_id TEXT,
          sf_display_text TEXT,
          sf_open_link TEXT,
          routing_reason TEXT NOT NULL
            CHECK(routing_reason IN
              ('sf_call_owner','sf_account_owner','sf_opp_owner','territory','unassigned')),
          slack_user_id TEXT,
          fallback_text TEXT NOT NULL,
          render_inputs_json TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL,
          expires_at TIMESTAMP,
          state_updated_at TIMESTAMP,
          UNIQUE(dedup_key, audience)
        );
        """,
    )


def migration_17_rich_card_actions(conn: sqlite3.Connection) -> None:
    """Action state keyed by the immutable snapshot, not the mutable lead. The partial
    UNIQUE on (snapshot_id, action) for 'draft' collapses a double-click or a Slack retry
    to ONE request (critic idempotency)."""
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS rich_card_actions (
          id TEXT PRIMARY KEY,
          snapshot_id TEXT NOT NULL,
          action TEXT NOT NULL CHECK(action IN ('draft','not_relevant')),
          nonce TEXT NOT NULL,
          requester_slack TEXT NOT NULL,
          state TEXT NOT NULL
            CHECK(state IN ('requested','accepted','rejected','blocked_expired')),
          detail TEXT,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL,
          UNIQUE(snapshot_id, action, nonce)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rich_draft_once
          ON rich_card_actions(snapshot_id) WHERE action='draft';
        """,
    )


def migration_18_contact_evidence(conn: sqlite3.Connection) -> None:
    """Forward-only contact-evidence lifecycle. Does NOT edit the legacy `contacts`
    table (that stays for the existing flows). Append-only: a re-verify inserts a new
    row and marks the prior 'superseded'; the current contact is the latest
    non-superseded row for a lead. Personal-provider rejection and official-domain
    binding are enforced in code (`campaign/policy.py`), recorded here as evidence."""
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS contact_evidence (
          id TEXT PRIMARY KEY,
          lead_id INTEGER NOT NULL,
          status TEXT NOT NULL
            CHECK(status IN
              ('verified','superseded','removed','unavailable','not_found')),
          contact_type TEXT
            CHECK(contact_type IS NULL OR contact_type IN ('named_direct','official_general')),
          name TEXT,
          title TEXT,
          email TEXT,
          official_evidence_url TEXT,
          official_domain TEXT,
          evidence_hash TEXT,
          first_verified_at TIMESTAMP,
          last_checked_at TIMESTAMP,
          last_verified_at TIMESTAMP,
          expires_at TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_contact_evidence_lead
          ON contact_evidence(lead_id, status);
        """,
    )


def migration_19_salesforce_activity_evidence(conn: sqlite3.Connection) -> None:
    """Persist exact Salesforce owner identity and typed completed-call evidence.

    Owner names are display text, not identity. The new scalar fields retain the
    Salesforce User id/email needed for an exact roster mapping. Activity rows are
    append-only lookup results so an outage never rewrites an older successful fact;
    preparation reads only the newest fresh row and fails closed on every other state.
    """
    _add_col(conn, "salesforce_matches", "owner_id TEXT")
    _add_col(conn, "salesforce_matches", "owner_email TEXT")
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS salesforce_activity_snapshots (
          id TEXT PRIMARY KEY,
          lead_id INTEGER NOT NULL REFERENCES leads(id),
          status TEXT NOT NULL
            CHECK(status IN ('verified_call','no_recent_call','unavailable')),
          activity_id TEXT,
          activity_type TEXT,
          completed_at TIMESTAMP,
          account_id TEXT,
          person_id TEXT,
          owner_user_id TEXT,
          owner_name TEXT,
          owner_email TEXT,
          owner_slack_id TEXT,
          roster_status TEXT NOT NULL
            CHECK(roster_status IN ('exact','unmapped','not_applicable')),
          record_link TEXT,
          checked_at TIMESTAMP NOT NULL,
          error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sf_activity_lead_checked
          ON salesforce_activity_snapshots(lead_id, checked_at DESC);
        """,
    )


def migration_20_complete_rich_snapshot_evidence(conn: sqlite3.Connection) -> None:
    """Add the remaining frozen evidence needed by thread/action consumers.

    These columns deliberately duplicate display/evidence facts. A rich-card reply
    must remain truthful after the mutable lead, contact, or CRM projection changes.
    The not-relevant uniqueness index also collapses Slack retries to one outcome.
    """
    for definition in (
        "source_item_id TEXT",
        "contact_evidence_id TEXT",
        "contact_title TEXT",
        "sf_activity_completed_at TIMESTAMP",
        "sf_activity_owner_user_id TEXT",
        "sf_activity_owner_email TEXT",
        "sf_activity_checked_at TIMESTAMP",
    ):
        _add_col(conn, "rich_card_snapshots", definition)
    _execute_script(
        conn,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rich_not_relevant_once
          ON rich_card_actions(snapshot_id) WHERE action='not_relevant';
        """,
    )


def migration_21_preparation_evidence_and_paid_calls(conn: sqlite3.Connection) -> None:
    """Store reviewed organization-kind evidence and paid-call preflight state.

    Runtime kind inference by organization name is intentionally excluded. Paid
    enrichment attempts are recorded before HTTP; an abandoned ``in_flight`` row is
    indeterminate and cannot be silently retried after restart.
    """
    _execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS organization_kind_evidence (
          id TEXT PRIMARY KEY,
          lead_id INTEGER NOT NULL REFERENCES leads(id),
          kind TEXT NOT NULL CHECK(kind IN ('school','school_district','city')),
          provenance TEXT NOT NULL CHECK(provenance IN ('source','nces','census','reviewed')),
          evidence_ref TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('verified','superseded','removed')),
          verified_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_org_kind_lead_status
          ON organization_kind_evidence(lead_id, status, verified_at DESC);

        CREATE TABLE IF NOT EXISTS paid_enrichment_attempts (
          id TEXT PRIMARY KEY,
          lead_id INTEGER NOT NULL REFERENCES leads(id),
          operation TEXT NOT NULL,
          request_key TEXT NOT NULL,
          attempt_no INTEGER NOT NULL,
          state TEXT NOT NULL
            CHECK(state IN ('in_flight','completed','failed','indeterminate')),
          started_at TIMESTAMP NOT NULL,
          finished_at TIMESTAMP,
          error TEXT,
          UNIQUE(request_key, attempt_no)
        );
        CREATE INDEX IF NOT EXISTS idx_paid_enrichment_request
          ON paid_enrichment_attempts(request_key, attempt_no DESC);
        """,
    )


def migration_22_freeze_contact_evidence_hash(conn: sqlite3.Connection) -> None:
    """Freeze the immutable contact evidence hash used by click-time vetoes."""
    _add_col(conn, "rich_card_snapshots", "contact_evidence_hash TEXT")


def migration_23_rich_snapshot_truth_and_retry_link(
    conn: sqlite3.Connection,
) -> None:
    """Freeze exact event/site truth and link queued outreach to its card action.

    The companion table deliberately has no foreign key: snapshot audit evidence must
    survive delete-based source reconciliation. ``award_dedup_key`` is source-qualified
    and policy-independent; immutable evidence versions may supersede one another while
    the delivery outbox continues to enforce one award/audience delivery.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rich_card_snapshot_truth (
             snapshot_id TEXT PRIMARY KEY,
             award_dedup_key TEXT NOT NULL,
             source_name TEXT NOT NULL,
             event_type TEXT NOT NULL
               CHECK(event_type IN ('award_announced','award_obligated')),
             event_amount REAL NOT NULL,
             event_verification_status TEXT NOT NULL,
             event_evidence_excerpt TEXT,
             event_evidence_hash TEXT NOT NULL,
             event_source_locator TEXT NOT NULL,
             official_website_evidence_url TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_rich_truth_award
             ON rich_card_snapshot_truth(award_dedup_key)"""
    )
    if "outreach_request_id" not in _column_names(conn, "rich_card_actions"):
        conn.execute(
            "ALTER TABLE rich_card_actions ADD COLUMN outreach_request_id TEXT"
        )


def migration_24_atomic_proactive_daily_slots(conn: sqlite3.Connection) -> None:
    """Serialize rich-card and future follow-up claims under one Pacific-day cap."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS proactive_daily_slots (
             audience TEXT NOT NULL,
             local_date TEXT NOT NULL,
             delivery_kind TEXT NOT NULL
               CHECK(delivery_kind IN ('rich_award','salesforce_followup')),
             delivery_key TEXT NOT NULL UNIQUE,
             reserved_at TIMESTAMP NOT NULL,
             PRIMARY KEY(audience,local_date)
           )"""
    )


def migration_25_typed_provenance_and_card_mode(conn: sqlite3.Connection) -> None:
    """Freeze WHY the website + contact email were trusted, and the card's mode.

    ``official_website_provenance`` and ``contact_domain_binding`` record the typed,
    non-heuristic reason each was accepted (NCES / approved directory / verified org
    page; org-site / authoritative-directory) so an audit can prove a card never
    trusted a name guess. ``card_mode`` distinguishes a draft-ready card from a
    Salesforce research-needed card, which must not offer an active draft action.
    All three are nullable so the (empty in production) rich tables upgrade cleanly.
    """
    if "official_website_provenance" not in _column_names(
        conn, "rich_card_snapshot_truth"
    ):
        conn.execute(
            "ALTER TABLE rich_card_snapshot_truth "
            "ADD COLUMN official_website_provenance TEXT"
        )
    if "contact_domain_binding" not in _column_names(conn, "rich_card_snapshot_truth"):
        conn.execute(
            "ALTER TABLE rich_card_snapshot_truth ADD COLUMN contact_domain_binding TEXT"
        )
    if "card_mode" not in _column_names(conn, "rich_card_snapshots"):
        conn.execute("ALTER TABLE rich_card_snapshots ADD COLUMN card_mode TEXT")


def migration_26_exact_nces_website(conn: sqlite3.Connection) -> None:
    """Home for the EXACT NCES-published official website — the only org->website evidence
    that may back a DRAFT-READY card (Chase, 2026-07-23; a heuristic ``_looks_official``
    website caps a card at research-needed).

    Nullable and forward-only (ADD COLUMN, O(1) metadata change, rollback-inert). NO
    runtime source populates it yet, so every current lead stays NULL and therefore
    research-only; a future AUTHORIZED authoritative fetch may set it, lighting up
    draft-ready cards. Old code that never reads the column is unaffected."""
    if "nces_website" not in _column_names(conn, "leads"):
        conn.execute("ALTER TABLE leads ADD COLUMN nces_website TEXT")
