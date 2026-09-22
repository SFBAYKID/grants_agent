"""The pre-approval summary must count named contacts and blame the right system.

On 2026-09-22, after ZoomInfo contacts were bought for 26 Oregon organizations,
Grant still told Kerry all 77 unmatched organizations would be "organization-only",
and said a clash between Grant's own rows "matches multiple Oregon records" in
Salesforce. Both came from this summary text. Offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from campaign_batch_support import CAMPAIGNS, campaign_link, insert_leads
from test_campaign_load_composition import NellyGateway
from test_campaign_load_composition import _writer_env as _writer_env  # noqa: F401

from grant_watch import db
from grant_watch.enrich.salesforce_campaign_batch import prepare_campaign_batch
from grant_watch.enrich.salesforce_campaign_batch_models import CampaignTargetRequest
from grant_watch.models import LeadGrade


def _lead_id(conn: object, entity: str) -> int:
    """The lowest (representative) Grant lead id for one organization name."""
    return int(
        conn.execute(  # type: ignore[attr-defined]
            "SELECT MIN(id) FROM leads WHERE entity_name=?", (entity,)
        ).fetchone()[0]
    )


@pytest.fixture
def summary(tmp_path: Path) -> str:
    """Kerry's shape: missing orgs, two with ZoomInfo people, one Grant-row clash."""
    conn = db.connect(tmp_path / "t.db")
    insert_leads(conn, "IL", LeadGrade.SILVER, 10, 0)
    # A second grant under the SAME name: a Grant-row collision, not Salesforce's.
    insert_leads(conn, "IL", LeadGrade.GOLD, 1, 9)
    for entity in ("IL Organization 001", "IL Organization 002"):
        db.save_vendor_contact(
            conn,
            _lead_id(conn, entity),
            "Pat Rivera",
            "Director of Technology",
            "privera@example.test",
            "",
            "12345",
            do_not_call=False,
        )
    gateway = NellyGateway()
    gateway.missing_names = {f"IL Organization {n:03d}" for n in range(1, 9)}
    batch = prepare_campaign_batch(
        conn,
        gateway,
        "TWORK",
        "CGRANTS",
        "1.0",
        "UREP",
        (
            CampaignTargetRequest(
                campaign_link("Campaign", CAMPAIGNS["IL"][0]),
                "IL",
                ("silver", "gold"),
            ),
        ),
    )
    assert batch.actions == ()
    return batch.summary


def test_the_summary_counts_the_new_leads_that_name_a_person(summary: str) -> None:
    """Never "all organization-only" when Grant holds contacts for some."""
    assert (
        "8 have no Salesforce record at all — approve creating them as new Leads "
        "so I can add them: 2 with a named contact Grant has on file and "
        "6 organization-only"
    ) in summary


def test_a_grant_row_clash_is_not_blamed_on_salesforce(summary: str) -> None:
    """The clash is named, and attributed to Grant's own data."""
    assert "IL Organization 009" in summary
    assert "this is Grant's own data, not Salesforce" in summary
    assert "match more than one Salesforce record" not in summary
