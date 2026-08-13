"""Forward-only migrations for evidence, provider, and lease safety boundaries.

These migrations add new state without relabeling historical rows.  In particular,
legacy organization projections are not backfilled as verified evidence: one old
``source_url`` cannot honestly prove every field that happened to be present.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return column names for one SQLite table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    """Add a forward-compatible column only when it is absent."""
    if definition.split()[0] not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migration_41_field_evidence_and_nces_site(conn: sqlite3.Connection) -> None:
    """Separate candidates from proven org facts and retain per-field provenance."""
    for definition in (
        "org_website_candidate TEXT",
        "nces_website_source_url TEXT",
        "nces_website_status TEXT",
        "nces_website_checked_at TIMESTAMP",
    ):
        _add_column(conn, "leads", definition)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS organization_field_evidence (
          id TEXT PRIMARY KEY,
          lead_id INTEGER NOT NULL REFERENCES leads(id),
          field_name TEXT NOT NULL CHECK(field_name IN
            ('website','general_email','phone','street','city','state','postal_code')),
          field_value TEXT NOT NULL,
          source_url TEXT NOT NULL,
          excerpt TEXT NOT NULL,
          evidence_hash TEXT NOT NULL,
          verifier_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('current','superseded')),
          verified_at TIMESTAMP NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ix_org_field_current
          ON organization_field_evidence(lead_id, field_name)
          WHERE status='current';
        CREATE INDEX IF NOT EXISTS ix_org_field_lead_verified
          ON organization_field_evidence(lead_id, verified_at DESC);
        """
    )


def migration_42_firecrawl_runtime_gateway(conn: sqlite3.Connection) -> None:
    """Add one durable budget, attempt ledger, and provider backoff boundary."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS firecrawl_runtime_periods (
          billing_period TEXT PRIMARY KEY,
          call_limit INTEGER NOT NULL CHECK(call_limit > 0),
          reserved_calls INTEGER NOT NULL DEFAULT 0 CHECK(reserved_calls >= 0),
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL,
          CHECK(reserved_calls <= call_limit)
        );
        CREATE TABLE IF NOT EXISTS firecrawl_runtime_attempts (
          id TEXT PRIMARY KEY,
          request_key TEXT NOT NULL UNIQUE,
          workflow TEXT NOT NULL,
          operation TEXT NOT NULL CHECK(operation IN ('search','scrape')),
          billing_period TEXT NOT NULL
            REFERENCES firecrawl_runtime_periods(billing_period),
          state TEXT NOT NULL CHECK(state IN
            ('in_flight','completed','failed','indeterminate','rate_limited')),
          started_at TIMESTAMP NOT NULL,
          finished_at TIMESTAMP,
          http_status INTEGER,
          retry_after_seconds REAL,
          error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_firecrawl_runtime_period_state
          ON firecrawl_runtime_attempts(billing_period, state, started_at);
        CREATE TABLE IF NOT EXISTS firecrawl_runtime_provider_state (
          provider TEXT PRIMARY KEY CHECK(provider='firecrawl'),
          blocked_until TIMESTAMP,
          reason TEXT,
          updated_at TIMESTAMP NOT NULL
        );
        """
    )


def migration_43_fenced_poll_lease(conn: sqlite3.Connection) -> None:
    """Turn the age-based polling mutex into a renewable fencing lease."""
    for definition in (
        "heartbeat_at TIMESTAMP",
        "expires_at TIMESTAMP",
        "fence_token INTEGER NOT NULL DEFAULT 0",
    ):
        _add_column(conn, "poll_locks", definition)
    # A pre-upgrade lock cannot prove its owner still exists. Mark it immediately
    # expired while retaining the row so the first takeover advances the token.
    conn.execute(
        """UPDATE poll_locks
              SET heartbeat_at=COALESCE(heartbeat_at,acquired_at),
                  expires_at=COALESCE(expires_at,acquired_at)"""
    )


def migration_44_starbridge_provenance(conn: sqlite3.Connection) -> None:
    """Rename and downgrade historical third-party Starbridge observations.

    Older aggregator rows shared ``source='rfp'`` with the official-page discovery
    parser and were marked verified. The raw payload's explicit aggregator marker
    is the non-heuristic discriminator; unrelated direct RFP rows are untouched.
    """
    candidates = """SELECT id FROM leads
        WHERE source='rfp' AND (
          LOWER(COALESCE(raw_json,'')) LIKE '%"aggregator": "starbridge"%'
          OR LOWER(COALESCE(raw_json,'')) LIKE '%"aggregator":"starbridge"%'
        )"""
    conn.execute(
        f"""UPDATE source_observations
               SET source='starbridge',verification_status='needs-testing'
             WHERE lead_id IN ({candidates})"""
    )
    conn.execute(
        f"""UPDATE funding_events
               SET verification_status='needs-testing',suppressed=1
             WHERE lead_id IN ({candidates})"""
    )
    conn.execute(f"UPDATE leads SET source='starbridge' WHERE id IN ({candidates})")


def migration_45_firecrawl_request_identity(conn: sqlite3.Connection) -> None:
    """Add exact retry identity without inventing it for historical attempts.

    Migration 42 stored only an opaque key containing a truncated digest. The full
    canonical request hash cannot be reconstructed from that key, so legacy rows stay
    NULL and remain preserved for spend accounting. Every post-upgrade gateway insert
    supplies both fields; the partial index supports exact-request retry checks.
    """
    _add_column(conn, "firecrawl_runtime_attempts", "request_hash TEXT")
    _add_column(
        conn,
        "firecrawl_runtime_attempts",
        "attempt_number INTEGER NOT NULL DEFAULT 1 CHECK(attempt_number > 0)",
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_firecrawl_runtime_request_attempt
             ON firecrawl_runtime_attempts(
               billing_period,request_hash,started_at DESC
             )
             WHERE request_hash IS NOT NULL"""
    )


def migration_46_quarantine_legacy_unbound_evidence(conn: sqlite3.Connection) -> None:
    """Keep pre-verifier contact/org projections out of strict truth surfaces.

    Historical ``verified`` and ``not_found`` labels predate typed local evidence and
    cannot prove either a positive or a clean exhaustive miss. They remain as rows for
    audit, but are downgraded to ``unverified`` and their completed one-shot marker is
    reopened for research. Organization projections survive only when a current
    field-evidence row proves the exact projected value.
    """
    # Rich-card contacts lived in a separate append-only table. Its historical hash
    # fingerprints the projected fact, not the parser proof, so no pre-v46 positive
    # can be promoted. Reopen its one-shot marker before quarantining the row.
    _add_column(conn, "contact_evidence", "field_evidence_json TEXT")
    conn.execute(
        """UPDATE paid_enrichment_attempts
              SET state='failed',error='legacy_rich_contact_requires_research'
            WHERE operation='contact_refresh' AND state='completed'
              AND lead_id IN (
                SELECT lead_id FROM contact_evidence WHERE status='verified'
              )"""
    )
    conn.execute(
        "UPDATE contact_evidence SET status='superseded' WHERE status='verified'"
    )

    # First quarantine malformed/missing JSON without calling json_extract on it.
    conn.execute(
        """UPDATE contacts
              SET contact_status='unverified',contact_provenance=NULL,provenance=NULL
            WHERE contact_status='verified'
              AND (field_evidence_json IS NULL OR json_valid(field_evidence_json)=0)"""
    )
    # Valid JSON still needs exact name+email evidence bound to the stored page.
    conn.execute(
        """UPDATE contacts
              SET contact_status='unverified',contact_provenance=NULL,provenance=NULL
            WHERE contact_status='verified' AND (
              COALESCE(json_extract(field_evidence_json,'$.name.field'),'') <> 'name'
              OR COALESCE(json_extract(field_evidence_json,'$.email.field'),'') <> 'email'
              OR TRIM(COALESCE(json_extract(field_evidence_json,'$.name.value'),''))
                   <> TRIM(COALESCE(name,''))
              OR LOWER(TRIM(COALESCE(
                   json_extract(field_evidence_json,'$.email.value'),'')))
                   <> LOWER(TRIM(COALESCE(email,'')))
              OR COALESCE(json_extract(
                   field_evidence_json,'$.name.source_url'),'')
                   <> COALESCE(source_url,'')
              OR COALESCE(json_extract(
                   field_evidence_json,'$.email.source_url'),'')
                   <> COALESCE(source_url,'')
              OR TRIM(COALESCE(json_extract(
                   field_evidence_json,'$.name.excerpt'),'')) = ''
              OR TRIM(COALESCE(json_extract(
                   field_evidence_json,'$.email.excerpt'),'')) = ''
              OR TRIM(COALESCE(json_extract(
                   field_evidence_json,'$.name.evidence_hash'),'')) = ''
              OR TRIM(COALESCE(json_extract(
                   field_evidence_json,'$.email.evidence_hash'),'')) = ''
              OR TRIM(COALESCE(json_extract(
                   field_evidence_json,'$.name.verifier_version'),'')) = ''
              OR TRIM(COALESCE(json_extract(
                   field_evidence_json,'$.email.verifier_version'),'')) = ''
              OR (TRIM(COALESCE(title,'')) <> '' AND (
                   COALESCE(json_extract(field_evidence_json,'$.title.field'),'')
                     <> 'title'
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.title.value'),''))
                        <> TRIM(COALESCE(title,''))
                   OR COALESCE(json_extract(
                        field_evidence_json,'$.title.source_url'),'')
                        <> COALESCE(source_url,'')
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.title.excerpt'),'')) = ''
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.title.evidence_hash'),'')) = ''
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.title.verifier_version'),'')) = ''
              ))
              OR (TRIM(COALESCE(phone,'')) <> '' AND (
                   COALESCE(json_extract(field_evidence_json,'$.phone.field'),'')
                     <> 'phone'
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.phone.value'),''))
                        <> TRIM(COALESCE(phone,''))
                   OR COALESCE(json_extract(
                        field_evidence_json,'$.phone.source_url'),'')
                        <> COALESCE(source_url,'')
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.phone.excerpt'),'')) = ''
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.phone.evidence_hash'),'')) = ''
                   OR TRIM(COALESCE(json_extract(
                        field_evidence_json,'$.phone.verifier_version'),'')) = ''
              ))
            )"""
    )
    # Every invalidated historical outcome must be callable again. This includes a
    # completed positive whose contact was just quarantined, not only an old negative;
    # otherwise the one-shot marker blocks research and callers can mistake missing
    # recalled evidence for a proven miss.
    conn.execute(
        """UPDATE paid_enrichment_attempts
              SET state='failed',error='legacy_contact_requires_research'
            WHERE operation='legacy_contact_enrichment' AND state='completed'
              AND NOT EXISTS (
                SELECT 1 FROM contacts c
                 WHERE c.lead_id=paid_enrichment_attempts.lead_id
                   AND c.contact_status='verified'
              )
              AND NOT EXISTS (
                SELECT 1 FROM contacts c
                 WHERE c.lead_id=paid_enrichment_attempts.lead_id
                   AND c.contact_status='linkedin_only'
              )
              AND NOT EXISTS (
                SELECT 1 FROM leads l
                JOIN organization_field_evidence e ON e.lead_id=l.id
                 WHERE l.id=paid_enrichment_attempts.lead_id
                   AND e.field_name='general_email' AND e.status='current'
                   AND e.field_value=l.org_general_email
              )"""
    )
    conn.execute(
        "UPDATE contacts SET contact_status='unverified' "
        "WHERE contact_status='not_found'"
    )

    projection_fields = {
        "general_email": "org_general_email",
        "phone": "org_phone",
        "street": "org_street",
        "city": "org_city",
        "state": "org_state",
        "postal_code": "org_postal_code",
        "website": "org_website",
    }
    for field_name, column in projection_fields.items():
        conn.execute(
            f"""UPDATE leads SET {column}=NULL
                  WHERE COALESCE({column},'') <> '' AND NOT EXISTS (
                    SELECT 1 FROM organization_field_evidence e
                     WHERE e.lead_id=leads.id AND e.field_name=?
                       AND e.status='current' AND e.field_value=leads.{column}
                  )""",
            (field_name,),
        )
    conn.execute(
        """UPDATE leads SET org_profile_status=NULL,org_profile_source_url=NULL
              WHERE NOT EXISTS (
                SELECT 1 FROM organization_field_evidence e
                 WHERE e.lead_id=leads.id AND e.status='current'
              )"""
    )

    # Defense in depth for scripts/tests that insert contacts without the typed writer.
    # CASE prevents json_extract from evaluating malformed JSON.
    verified_invalid = """CASE
      WHEN NEW.field_evidence_json IS NULL THEN 1
      WHEN json_valid(NEW.field_evidence_json)=0 THEN 1
      ELSE (
        COALESCE(json_extract(NEW.field_evidence_json,'$.name.field'),'') <> 'name'
        OR COALESCE(json_extract(NEW.field_evidence_json,'$.email.field'),'') <> 'email'
        OR TRIM(COALESCE(json_extract(NEW.field_evidence_json,'$.name.value'),''))
             <> TRIM(COALESCE(NEW.name,''))
        OR LOWER(TRIM(COALESCE(
             json_extract(NEW.field_evidence_json,'$.email.value'),'')))
             <> LOWER(TRIM(COALESCE(NEW.email,'')))
        OR COALESCE(json_extract(
             NEW.field_evidence_json,'$.name.source_url'),'')
             <> COALESCE(NEW.source_url,'')
        OR COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.source_url'),'')
             <> COALESCE(NEW.source_url,'')
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.name.excerpt'),'')) = ''
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.excerpt'),'')) = ''
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.name.evidence_hash'),'')) = ''
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.evidence_hash'),'')) = ''
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.name.verifier_version'),'')) = ''
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.verifier_version'),'')) = ''
        OR (TRIM(COALESCE(NEW.title,'')) <> '' AND (
             COALESCE(json_extract(NEW.field_evidence_json,'$.title.field'),'')
               <> 'title'
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.value'),''))
                  <> TRIM(COALESCE(NEW.title,''))
             OR COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.source_url'),'')
                  <> COALESCE(NEW.source_url,'')
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.excerpt'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.evidence_hash'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.verifier_version'),'')) = ''
        ))
        OR (TRIM(COALESCE(NEW.phone,'')) <> '' AND (
             COALESCE(json_extract(NEW.field_evidence_json,'$.phone.field'),'')
               <> 'phone'
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.phone.value'),''))
                  <> TRIM(COALESCE(NEW.phone,''))
             OR COALESCE(json_extract(
                  NEW.field_evidence_json,'$.phone.source_url'),'')
                  <> COALESCE(NEW.source_url,'')
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.phone.excerpt'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.phone.evidence_hash'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.phone.verifier_version'),'')) = ''
        ))
      ) END"""
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS contacts_verified_evidence_insert
             BEFORE INSERT ON contacts
             WHEN NEW.contact_status='verified' AND ({verified_invalid})
             BEGIN
               SELECT RAISE(ABORT,'verified contact requires exact typed evidence');
             END"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS contacts_verified_evidence_update
             BEFORE UPDATE ON contacts
             WHEN NEW.contact_status='verified' AND ({verified_invalid})
             BEGIN
               SELECT RAISE(ABORT,'verified contact requires exact typed evidence');
             END"""
    )

    rich_invalid = """CASE
      WHEN NEW.field_evidence_json IS NULL THEN 1
      WHEN json_valid(NEW.field_evidence_json)=0 THEN 1
      ELSE (
        COALESCE(NEW.contact_type,'') NOT IN ('named_direct','official_general')
        OR TRIM(COALESCE(NEW.email,'')) = ''
        OR TRIM(COALESCE(NEW.official_evidence_url,'')) = ''
        OR TRIM(COALESCE(NEW.official_domain,'')) = ''
        OR TRIM(COALESCE(NEW.evidence_hash,'')) = ''
        OR COALESCE(json_extract(NEW.field_evidence_json,'$.email.field'),'')
             <> 'email'
        OR LOWER(TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.value'),'')))
             <> LOWER(TRIM(COALESCE(NEW.email,'')))
        OR COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.source_url'),'')
             <> COALESCE(NEW.official_evidence_url,'')
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.excerpt'),'')) = ''
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.evidence_hash'),'')) = ''
        OR TRIM(COALESCE(json_extract(
             NEW.field_evidence_json,'$.email.verifier_version'),'')) = ''
        OR (NEW.contact_type='named_direct' AND (
             TRIM(COALESCE(NEW.name,'')) = ''
             OR COALESCE(json_extract(
                  NEW.field_evidence_json,'$.name.field'),'') <> 'name'
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.name.value'),''))
                  <> TRIM(COALESCE(NEW.name,''))
             OR COALESCE(json_extract(
                  NEW.field_evidence_json,'$.name.source_url'),'')
                  <> COALESCE(NEW.official_evidence_url,'')
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.name.excerpt'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.name.evidence_hash'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.name.verifier_version'),'')) = ''
        ))
        OR (NEW.contact_type='official_general' AND (
             TRIM(COALESCE(NEW.name,'')) <> ''
             OR TRIM(COALESCE(NEW.title,'')) <> ''
        ))
        OR (TRIM(COALESCE(NEW.title,'')) <> '' AND (
             COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.field'),'') <> 'title'
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.value'),''))
                  <> TRIM(COALESCE(NEW.title,''))
             OR COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.source_url'),'')
                  <> COALESCE(NEW.official_evidence_url,'')
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.excerpt'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.evidence_hash'),'')) = ''
             OR TRIM(COALESCE(json_extract(
                  NEW.field_evidence_json,'$.title.verifier_version'),'')) = ''
        ))
      ) END"""
    for operation in ("INSERT", "UPDATE"):
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS contact_evidence_typed_{operation.lower()}
                 BEFORE {operation} ON contact_evidence
                 WHEN NEW.status='verified' AND ({rich_invalid})
                 BEGIN
                   SELECT RAISE(ABORT,
                     'rich contact requires exact typed evidence');
                 END"""
        )
