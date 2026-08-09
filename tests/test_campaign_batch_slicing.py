"""Slicing a state/tier selection that exceeds Salesforce's 200-record limit.

Split from test_salesforce_campaign_batches.py at the 1000-line cap. These cover the
request that dead-ended an SDR: a whole tier is routinely larger than one collection
call, and the old code told her to "refine the request" using filters the tool does
not have. The invariant worth protecting is that slices partition the selection —
every organization in exactly one batch, no gap and no duplicate — and that the rep
is always told which batch they are looking at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich.salesforce_campaign_batch import prepare_campaign_batch
from grant_watch.enrich.salesforce_campaign_batch_models import CampaignTargetRequest
from grant_watch.models import LeadGrade
from campaign_batch_support import (
    CAMPAIGNS,
    BatchGateway,
    campaign_link as _link,
    insert_leads as _insert_leads,
)


@pytest.fixture(autouse=True)
def _writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow only the offline test channel and fake writer hostname."""
    monkeypatch.setenv("GRANT_SALESFORCE_WRITE_CHANNEL_IDS", "CGRANTS")
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.salesforce.test"
    )
    monkeypatch.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")


def test_a_selection_over_the_collection_limit_is_sliced_not_refused(
    tmp_path: Path,
) -> None:
    """201 organizations become two ordered batches, and the rep is told so.

    This used to raise "refine the request below the 200-record Salesforce limit" —
    advice the tool cannot take, because its only filters are state and grade and
    both are already at their finest. An SDR asking for a whole tier was told to do
    something impossible, which is how the request dead-ended.
    """
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 201, 0)

    first = prepare_campaign_batch(
        conn,
        BatchGateway(),
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",)
            ),
        ),
    )
    assert "batch 1 of 2" in first.summary
    assert "201 organizations in total" in first.summary

    second = prepare_campaign_batch(
        conn,
        BatchGateway(),
        "TWORK",
        "CGRANTS",
        "123.4",
        "UREP",
        (
            CampaignTargetRequest(
                _link("Campaign", CAMPAIGNS["IL"][0]), "IL", ("gold",), slice_index=1
            ),
        ),
    )
    assert "batch 2 of 2" in second.summary

    # Every organization appears in exactly one slice — no gap, no duplicate.
    first_keys = {
        row[0]
        for row in conn.execute(
            "SELECT canonical_entity_key FROM crm_campaign_batch_items i "
            "JOIN crm_campaign_batch_targets t ON t.id=i.target_id WHERE t.batch_id=?",
            (first.batch_id,),
        )
    }
    second_keys = {
        row[0]
        for row in conn.execute(
            "SELECT canonical_entity_key FROM crm_campaign_batch_items i "
            "JOIN crm_campaign_batch_targets t ON t.id=i.target_id WHERE t.batch_id=?",
            (second.batch_id,),
        )
    }
    assert len(first_keys) == 200
    assert len(second_keys) == 1
    assert not (first_keys & second_keys)
    assert len(first_keys | second_keys) == 201


def test_a_slice_beyond_the_end_is_refused_rather_than_returning_nothing(
    tmp_path: Path,
) -> None:
    """Asking for batch 3 of 2 must say so, not silently prepare an empty batch."""
    conn = db.connect(tmp_path / "batch.db")
    _insert_leads(conn, "IL", LeadGrade.GOLD, 201, 0)
    with pytest.raises(ValueError, match="does not exist"):
        prepare_campaign_batch(
            conn,
            BatchGateway(),
            "TWORK",
            "CGRANTS",
            "123.4",
            "UREP",
            (
                CampaignTargetRequest(
                    _link("Campaign", CAMPAIGNS["IL"][0]),
                    "IL",
                    ("gold",),
                    slice_index=2,
                ),
            ),
        )
