"""Migrations 14-24 for the rich award-card campaign: fresh apply, historical upgrade,
data preservation through the posts rebuild, and rollback inertness.

The rich card MUST write a posts row (thread attribution runs through the posts table),
so the posts.kind CHECK widening (v15) repeats the migration-13 rebuild recipe. These
tests guard that the rebuild preserves ids (engagement.post_id references) and that a
rolled-back reader is inert to the new tables/columns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db, migrations
from grant_watch.migrations import MIGRATIONS
from tests.contact_support import verified_contact_evidence

# The head schema version, maintained BY HAND. Deriving it from MIGRATIONS[-1]
# makes every assertion below tautological — MAX(version) from schema_migrations
# cannot differ from the migration list while migrations apply at all. The literal
# is the point: adding a migration must fail this file until someone bumps it
# deliberately, which is the schema-change review gate.
HEAD_VERSION = 48


def test_fresh_database_reaches_current_head_with_all_rich_tables(
    tmp_path: Path,
) -> None:
    """A brand-new database applies every migration through the reviewed head."""
    conn = db.connect(tmp_path / "fresh.db")
    assert (
        conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == HEAD_VERSION
    )
    for table in (
        "rich_card_snapshots",
        "rich_card_actions",
        "contact_evidence",
        "salesforce_activity_snapshots",
        "organization_kind_evidence",
        "paid_enrichment_attempts",
        "firecrawl_runtime_attempts",
        "firecrawl_runtime_periods",
        "firecrawl_runtime_provider_state",
        "rich_card_snapshot_truth",
        "proactive_daily_slots",
    ):
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (table,)
        ).fetchone(), table
    posts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='posts'"
    ).fetchone()[0]
    assert "rich_award" in posts_sql
    assert "snapshot_id" in {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    assert "snapshot_id" in {
        r[1] for r in conn.execute("PRAGMA table_info(notification_outbox)")
    }
    assert "state" in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"last_confirmed_run_id", "last_confirmed_at", "nces_website"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(leads)")
    }
    assert {"heartbeat_at", "expires_at", "fence_token"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(poll_locks)")
    }
    assert {"owner_id", "owner_email"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(salesforce_matches)")
    }
    assert {
        "contact_title",
        "contact_evidence_hash",
        "sf_activity_owner_email",
        "source_item_id",
        "card_mode",
    } <= {r[1] for r in conn.execute("PRAGMA table_info(rich_card_snapshots)")}
    assert {
        "official_website_provenance",
        "contact_domain_binding",
    } <= {r[1] for r in conn.execute("PRAGMA table_info(rich_card_snapshot_truth)")}
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _at_version(path: Path, target: int) -> sqlite3.Connection:
    """Build one real historical schema from the ordered migration functions."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE schema_migrations (
             version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TIMESTAMP NOT NULL
           )"""
    )
    for migration in migrations.MIGRATIONS:
        if migration.version > target:
            break
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?)",
            (migration.version, migration.name, "2026-07-01T00:00:00+00:00"),
        )
    conn.commit()
    return conn


def _at_v13(path: Path) -> sqlite3.Connection:
    """Build the real deployed v13 schema for data-preservation assertions."""
    return _at_version(path, 13)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8, 9, 13])
def test_upgrade_from_each_supported_historical_schema(
    tmp_path: Path, version: int
) -> None:
    """Every ledger version that existed upgrades to head with integrity intact."""
    path = tmp_path / f"v{version}.db"
    historical = _at_version(path, version)
    historical.close()
    upgraded = db.connect(path)
    assert (
        upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == HEAD_VERSION
    )
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v13_upgrade_preserves_posts_ids_and_engagement(tmp_path: Path) -> None:
    """The v15 posts rebuild must preserve every post id (engagement.post_id references)
    and every row — the exact guarantee migration 13 established."""
    path = tmp_path / "hist.db"
    seed = _at_v13(path)
    # a lead + a post + an engagement row referencing it (the FK the rebuild must keep)
    seed.execute(
        "INSERT INTO leads (id, source, source_item_id, entity_name, status) "
        "VALUES (7, 'usaspending:16.071', 'A', 'Test District', 'new')"
    )
    seed.execute(
        "INSERT INTO posts (id, kind, lead_id, channel, ts, posted_at) "
        "VALUES (42, 'nugget', 7, 'C1', '1.0', '2026-07-01T00:00:00+00:00')"
    )
    seed.execute(
        "INSERT INTO engagement (post_id, slack_user, kind, at) "
        "VALUES (42, 'U1', 'reply', '2026-07-01T00:01:00+00:00')"
    )
    seed.commit()
    seed.close()

    upgraded = db.connect(path)
    assert (
        upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == HEAD_VERSION
    )
    assert (
        upgraded.execute("SELECT kind FROM posts WHERE id=42").fetchone()[0] == "nugget"
    )
    assert upgraded.execute("SELECT post_id FROM engagement").fetchone()[0] == 42, (
        "engagement.post_id reference broken by rebuild"
    )
    # the widened CHECK now admits the rich kind
    upgraded.execute(
        "INSERT INTO posts (kind, channel, ts) VALUES ('rich_award','C1','2.0')"
    )
    upgraded.commit()
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_new_columns_are_nullable_so_legacy_inserts_still_work(tmp_path: Path) -> None:
    """Rollback safety: old code inserts posts/outbox rows WITHOUT snapshot_id. The new
    columns must be nullable-with-default so those inserts still succeed."""
    conn = db.connect(tmp_path / "rb.db")
    # a legacy-shaped posts insert (no snapshot_id, no rich kind)
    conn.execute(
        "INSERT INTO posts (kind, channel, ts, posted_at) "
        "VALUES ('nugget','C1','9.9','2026-07-01T00:00:00+00:00')"
    )
    conn.commit()
    row = conn.execute("SELECT snapshot_id FROM posts WHERE ts='9.9'").fetchone()
    assert row["snapshot_id"] is None


def test_the_migration_list_head_matches_this_module_s_literal() -> None:
    """The one place the hand-maintained literal is checked against reality.

    This is deliberately the ONLY link between the literal and the code. Adding a
    migration fails here and nowhere else, forcing a human to look at the new
    migration rather than letting three data-preservation tests silently re-point
    themselves at whatever version happens to be current.
    """
    assert MIGRATIONS[-1].version == HEAD_VERSION


def test_v42_firecrawl_attempts_upgrade_without_fabricated_request_hash(
    tmp_path: Path,
) -> None:
    """Legacy spend remains counted while unknowable exact identity stays NULL."""
    path = tmp_path / "v42-firecrawl.db"
    legacy = _at_version(path, 42)
    legacy.execute(
        """INSERT INTO firecrawl_runtime_periods
             (billing_period,call_limit,reserved_calls,created_at,updated_at)
           VALUES ('2026-08',10,1,'2026-08-12T00:00:00+00:00',
                   '2026-08-12T00:00:00+00:00')"""
    )
    legacy_prefix = "a" * 24
    legacy.execute(
        """INSERT INTO firecrawl_runtime_attempts
             (id,request_key,workflow,operation,billing_period,state,started_at)
           VALUES ('old',?,'legacy','search','2026-08','indeterminate',
                   '2026-08-12T00:00:00+00:00')""",
        (f"search:{legacy_prefix}:opaque",),
    )
    legacy.commit()
    legacy.close()

    upgraded = db.connect(path)
    row = upgraded.execute(
        """SELECT request_hash,attempt_number,state
             FROM firecrawl_runtime_attempts WHERE id='old'"""
    ).fetchone()
    assert tuple(row) == (None, 1, "indeterminate")
    assert (
        upgraded.execute(
            "SELECT reserved_calls FROM firecrawl_runtime_periods WHERE billing_period='2026-08'"
        ).fetchone()[0]
        == 1
    )
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v45_upgrade_quarantines_unbound_contact_and_org_claims(
    tmp_path: Path,
) -> None:
    """Pre-verifier positives/negatives stay auditable but cannot short-circuit truth."""
    path = tmp_path / "v45-legacy-evidence.db"
    legacy = _at_version(path, 45)
    legacy.executemany(
        """INSERT INTO leads
             (id,source,source_item_id,entity_name,state,status,org_general_email,
              org_phone,org_street,org_city,org_state,org_postal_code,
              org_profile_status,org_profile_source_url)
           VALUES (?, 'legacy', ?, ?, 'CA', 'new', 'info@wrong.test',
                   '555-111-2222','1 Wrong Way','Wrongtown','TX','99999',
                   'found','https://wrong.test')""",
        (
            (1, "positive", "Legacy Positive"),
            (2, "negative", "Legacy Negative"),
        ),
    )
    legacy.execute(
        """INSERT INTO contacts
             (lead_id,name,email,source_url,contact_status,contact_provenance,provenance)
           VALUES (1,'Jane False','jane@wrong.test','https://wrong.test',
                   'verified','page_verified','page_verified')"""
    )
    legacy.execute(
        "INSERT INTO contacts(lead_id,contact_status) VALUES (2,'not_found')"
    )
    legacy.execute(
        """INSERT INTO contact_evidence
             (id,lead_id,status,contact_type,name,title,email,official_evidence_url,
              official_domain,evidence_hash,last_checked_at,last_verified_at,expires_at)
           VALUES ('legacy-rich',1,'verified','named_direct','Jane False','Director',
                   'jane@wrong.test','https://wrong.test','wrong.test','legacy-hash',
                   '2026-08-01','2026-08-01','2026-09-01')"""
    )
    legacy.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES ('old-negative',2,'legacy_contact_enrichment','legacy-contact:2',
                   1,'completed','2026-08-01','2026-08-01')"""
    )
    legacy.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES ('old-positive',1,'legacy_contact_enrichment','legacy-contact:1',
                   1,'completed','2026-08-01','2026-08-01')"""
    )
    legacy.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES ('old-rich',1,'contact_refresh','rich-contact:1:2026-08-01',
                   1,'completed','2026-08-01','2026-08-01')"""
    )
    legacy.commit()
    legacy.close()

    upgraded = db.connect(path)
    assert [
        tuple(row)
        for row in upgraded.execute(
            "SELECT lead_id,contact_status,provenance FROM contacts ORDER BY lead_id"
        )
    ] == [(1, "unverified", None), (2, "unverified", None)]
    assert upgraded.execute(
        "SELECT state,error FROM paid_enrichment_attempts WHERE id='old-negative'"
    ).fetchone()[:] == ("failed", "legacy_contact_requires_research")
    assert upgraded.execute(
        "SELECT state,error FROM paid_enrichment_attempts WHERE id='old-positive'"
    ).fetchone()[:] == ("failed", "legacy_contact_requires_research")
    assert upgraded.execute(
        "SELECT status,field_evidence_json FROM contact_evidence WHERE id='legacy-rich'"
    ).fetchone()[:] == ("superseded", None)
    assert upgraded.execute(
        "SELECT state,error FROM paid_enrichment_attempts WHERE id='old-rich'"
    ).fetchone()[:] == ("failed", "legacy_rich_contact_requires_research")
    for lead in upgraded.execute("SELECT * FROM leads ORDER BY id"):
        assert lead["org_general_email"] is None
        assert lead["org_phone"] is None
        assert lead["org_street"] is None
        assert lead["org_profile_status"] is None
        assert lead["org_profile_source_url"] is None


def test_verified_contact_trigger_rejects_direct_untyped_writes(tmp_path: Path) -> None:
    """Direct SQL cannot recreate the legacy verified-without-proof state."""
    conn = db.connect(tmp_path / "trigger.db")
    conn.execute(
        "INSERT INTO leads(id,source,source_item_id,entity_name) "
        "VALUES (1,'test','one','One District')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="exact typed evidence"):
        conn.execute(
            """INSERT INTO contacts(lead_id,name,email,contact_status)
               VALUES (1,'Jane False','jane@example.test','verified')"""
        )
    minimal = (
        '{"name":{"field":"name","value":"Jane False",'
        '"source_url":"https://example.test"},'
        '"email":{"field":"email","value":"jane@example.test",'
        '"source_url":"https://example.test"}}'
    )
    with pytest.raises(sqlite3.IntegrityError, match="exact typed evidence"):
        conn.execute(
            """INSERT INTO contacts
                 (lead_id,name,email,source_url,contact_status,field_evidence_json)
               VALUES (1,'Jane False','jane@example.test','https://example.test',
                       'verified',?)""",
            (minimal,),
        )


def test_verified_contact_trigger_rejects_unproved_optional_field_mutation(
    tmp_path: Path,
) -> None:
    """A later direct update cannot attach an unproved title to verified facts."""
    conn = db.connect(tmp_path / "optional-field-trigger.db")
    conn.execute(
        "INSERT INTO leads(id,source,source_item_id,entity_name) "
        "VALUES (1,'test','one','One District')"
    )
    source_url = "https://district.example.test/staff"
    db.save_contact(
        conn,
        1,
        "Jane True",
        "",
        "jane@district.example.test",
        "",
        source_url,
        "high",
        field_evidence=verified_contact_evidence(
            "Jane True", "jane@district.example.test", source_url
        ),
    )

    with pytest.raises(sqlite3.IntegrityError, match="exact typed evidence"):
        conn.execute("UPDATE contacts SET title='Invented' WHERE lead_id=1")


def test_rich_contact_trigger_rejects_legacy_status_only_insert(tmp_path: Path) -> None:
    """The separate rich-card evidence table cannot bypass typed parser proof."""
    conn = db.connect(tmp_path / "rich-contact-trigger.db")
    conn.execute(
        "INSERT INTO leads(id,source,source_item_id,entity_name) "
        "VALUES (1,'test','one','One District')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="exact typed evidence"):
        conn.execute(
            """INSERT INTO contact_evidence
                 (id,lead_id,status,contact_type,name,email,official_evidence_url,
                  official_domain,evidence_hash)
               VALUES ('legacy',1,'verified','named_direct','Jane False',
                       'jane@example.test','https://example.test','example.test','x')"""
        )


def test_v45_mixed_invalid_verified_and_vendor_rows_reopen_research(
    tmp_path: Path,
) -> None:
    """An unrelated vendor row cannot keep an invalid page lookup completed forever."""
    path = tmp_path / "v45-mixed-contact.db"
    legacy = _at_version(path, 45)
    legacy.execute(
        "INSERT INTO leads(id,source,source_item_id,entity_name) "
        "VALUES (1,'legacy','one','One District')"
    )
    legacy.execute(
        "INSERT INTO leads(id,source,source_item_id,entity_name) "
        "VALUES (2,'legacy','two','Two District')"
    )
    legacy.execute(
        """INSERT INTO leads
             (id,source,source_item_id,entity_name,org_general_email,
              org_profile_status,org_profile_source_url)
           VALUES (3,'legacy','three','Three District','legacy@three.test',
                   'found','https://three.test/contact')"""
    )
    legacy.execute(
        """INSERT INTO leads
             (id,source,source_item_id,entity_name,org_general_email,
              org_profile_status,org_profile_source_url)
           VALUES (4,'legacy','four','Four District','info@four.test',
                   'found','https://four.test/contact')"""
    )
    legacy.execute(
        """INSERT INTO organization_field_evidence
             (id,lead_id,field_name,field_value,source_url,excerpt,evidence_hash,
              verifier_version,status,verified_at)
           VALUES ('bound-org',4,'general_email','info@four.test',
                   'https://four.test/contact','Email info@four.test','hash',
                   'field-evidence-v2','current','2026-08-01')"""
    )
    legacy.execute(
        """INSERT INTO contacts
             (lead_id,name,email,source_url,contact_status,provenance)
           VALUES (1,'Jane False','jane@wrong.test','https://wrong.test',
                   'verified','page_verified')"""
    )
    legacy.execute(
        """INSERT INTO contacts
             (lead_id,name,email,contact_status,provenance,vendor_person_id)
           VALUES (1,'Vendor Person','vendor@example.test','vendor_licensed',
                   'vendor_licensed','vendor-1')"""
    )
    legacy.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES ('completed',1,'legacy_contact_enrichment','legacy-contact:1',1,
                   'completed','2026-08-01','2026-08-01')"""
    )
    legacy.executemany(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES (?,?, 'legacy_contact_enrichment',?,1,'completed',
                   '2026-08-01','2026-08-01')""",
        (
            ("org-only-completed", 3, "legacy-contact:3"),
            ("bound-org-completed", 4, "legacy-contact:4"),
        ),
    )
    valid_url = "https://two.example.test/staff"
    db.save_contact(
        legacy,
        2,
        "Jane True",
        "",
        "jane@two.example.test",
        "",
        valid_url,
        "high",
        field_evidence=verified_contact_evidence(
            "Jane True", "jane@two.example.test", valid_url
        ),
    )
    legacy.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES ('valid-completed',2,'legacy_contact_enrichment','legacy-contact:2',1,
                   'completed','2026-08-01','2026-08-01')"""
    )
    legacy.commit()
    legacy.close()

    upgraded = db.connect(path)
    assert [
        row[0]
        for row in upgraded.execute("SELECT contact_status FROM contacts ORDER BY id")
    ] == ["unverified", "vendor_licensed", "verified"]
    assert (
        upgraded.execute(
            "SELECT state FROM paid_enrichment_attempts WHERE id='completed'"
        ).fetchone()[0]
        == "failed"
    )
    assert (
        upgraded.execute(
            "SELECT state FROM paid_enrichment_attempts WHERE id='valid-completed'"
        ).fetchone()[0]
        == "completed"
    )
    assert (
        upgraded.execute("SELECT org_general_email FROM leads WHERE id=3").fetchone()[0]
        is None
    )
    assert (
        upgraded.execute(
            "SELECT state FROM paid_enrichment_attempts WHERE id='org-only-completed'"
        ).fetchone()[0]
        == "failed"
    )
    assert (
        upgraded.execute(
            "SELECT state FROM paid_enrichment_attempts WHERE id='bound-org-completed'"
        ).fetchone()[0]
        == "completed"
    )
