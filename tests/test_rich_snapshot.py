"""Immutable rich-card snapshot freeze/load and stable dedup tests."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from grant_watch import db
from grant_watch.campaign.routing import Route, RoutingReason
from grant_watch.campaign.snapshot import SnapshotDraft, freeze, load


def _draft(**changes: object) -> SnapshotDraft:
    """Build a complete frozen-card fixture."""
    base = SnapshotDraft(
        audience="CGRANTS",
        lead_id=1,
        event_id=2,
        observation_id=3,
        run_id=4,
        source_item_id="award-123",
        canonical_entity_key="montebello usd|CA",
        award_identity="award-123",
        tier="gold",
        entity_name="Montebello Unified School District",
        entity_kind="school_district",
        entity_kind_provenance="nces",
        state="CA",
        state_provenance="usaspending:16.071",
        program="SVPP",
        amount=500_000.0,
        award_date="2026-06-01",
        award_date_precision="day",
        spend_window_start="2025-10-10",
        spend_window_end="2028-09-30",
        award_url="https://www.usaspending.gov/award/award-123",
        official_website="https://www.montebello.k12.ca.us",
        contact_evidence_id="contact-1",
        contact_name="Jon Smith",
        contact_title="IT Director",
        contact_type="named_direct",
        contact_email="jon@montebello.k12.ca.us",
        contact_evidence_url="https://www.montebello.k12.ca.us/staff",
        contact_verified_at="2026-07-20T00:00:00+00:00",
        contact_expires_at="2026-08-19T00:00:00+00:00",
        sf_lookup_status="exact_match",
        sf_account_id="001000000000001AAA",
        sf_open_opp_id="006000000000001AAA",
        sf_activity_id="00T000000000001AAA",
        sf_activity_completed_at="2026-07-15T12:00:00+00:00",
        sf_activity_owner_user_id="005000000000001AAA",
        sf_activity_owner_email="anthony@monarchconnected.com",
        sf_activity_checked_at="2026-07-22T12:00:00+00:00",
        sf_display_text="Completed call recorded 7 days ago.",
        sf_open_link="https://sf.test/lightning/r/Account/001/view",
        route=Route(RoutingReason.SF_CALL_OWNER, "U01DFJWQQJ3"),
        fallback_text="GOLD award for Montebello Unified School District.",
        expires_at="2026-08-19T00:00:00+00:00",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _conn(path: Path) -> sqlite3.Connection:
    """Create a migrated database with one lead for snapshot fixtures."""
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO leads(id,source,source_item_id,entity_name,status) "
        "VALUES (1,'fixture','award-123','Montebello USD','new')"
    )
    conn.commit()
    return conn


def test_freeze_and_load_preserve_every_fact(tmp_path: Path) -> None:
    """A loaded snapshot is byte-for-byte derived from the frozen rendering inputs."""
    conn = _conn(tmp_path / "snapshot.db")
    original = _draft()
    frozen, created = freeze(
        conn, original, now=datetime(2026, 7, 22, 18, tzinfo=timezone.utc)
    )
    assert created is True
    assert frozen.draft == original
    assert load(conn, frozen.id) == frozen


def test_mutable_lead_change_cannot_change_snapshot(tmp_path: Path) -> None:
    """Later event/entity/contact projection changes do not affect frozen actions."""
    conn = _conn(tmp_path / "immutable.db")
    frozen, _ = freeze(conn, _draft())
    conn.execute(
        "UPDATE leads SET entity_name='Different Entity',current_event_id=999 WHERE id=1"
    )
    conn.commit()
    loaded = load(conn, frozen.id)
    assert loaded is not None
    assert loaded.draft.entity_name == "Montebello Unified School District"
    assert loaded.draft.event_id == 2
    assert loaded.draft.contact_email == "jon@montebello.k12.ca.us"


def test_policy_change_or_event_surrogate_does_not_repost_same_award(
    tmp_path: Path,
) -> None:
    """Stable award identity wins over mutable policy/event/lead surrogate ids."""
    conn = _conn(tmp_path / "dedup.db")
    first, created = freeze(conn, _draft())
    second, created_again = freeze(
        conn,
        _draft(
            lead_id=999,
            event_id=888,
            fallback_text="New renderer wording that must not create another delivery.",
        ),
    )
    assert created is True and created_again is False
    assert second.id == first.id
    assert second.draft.fallback_text == first.draft.fallback_text


def test_same_award_may_be_frozen_for_a_distinct_audience(tmp_path: Path) -> None:
    """Audience is an explicit delivery boundary, not hidden inside the award hash."""
    conn = _conn(tmp_path / "audience.db")
    first, _ = freeze(conn, _draft(audience="CPROD"))
    second, created = freeze(conn, _draft(audience="CPLAYGROUND"))
    assert created is True
    assert second.id != first.id
