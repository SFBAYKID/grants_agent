"""Bulk Salesforce organization-resolution tests for Campaign batches."""

from __future__ import annotations

import pytest

from grant_watch.enrich import salesforce_campaigns as campaigns
from grant_watch.enrich.salesforce_campaign_gateway import (
    API_VERSION,
    SalesforceCampaignGateway,
)
from campaign_batch_support import campaign_link

CAMPAIGN_ID = "701000000000001"


def test_record_links_validate_host_object_and_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pasted links cannot cross orgs or smuggle an Account as a Campaign Member."""
    monkeypatch.setenv(
        "SALESFORCE_WRITE_MY_DOMAIN_URL", "https://writer.salesforce.test"
    )
    assert campaigns.parse_record_link(
        campaign_link("Campaign", CAMPAIGN_ID), {"Campaign"}
    ) == ("Campaign", CAMPAIGN_ID)
    with pytest.raises(ValueError, match="configured Salesforce org"):
        campaigns.parse_record_link(
            f"https://evil.test/lightning/r/Campaign/{CAMPAIGN_ID}/view", {"Campaign"}
        )
    with pytest.raises(ValueError, match="cannot be used"):
        campaigns.parse_record_link(
            campaign_link("Account", "001000000000001"), {"Lead", "Contact"}
        )


def test_bulk_organization_resolution_honors_salesforce_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunked exact resolution consumes bounded next-page URLs before previewing."""
    gateway = SalesforceCampaignGateway()
    paths: list[str] = []

    def fake_get(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
        """Return one paginated Lead query and empty related-object queries."""
        paths.append(path)
        query = str((params or {}).get("q") or "")
        if path == "query" and "FROM Lead" in query:
            return {
                "records": [],
                "done": False,
                "nextRecordsUrl": f"/services/data/{API_VERSION}/query/next",
            }
        if path == "query/next":
            return {
                "records": [
                    {
                        "Id": "00Q000000000001",
                        "Name": "Person",
                        "Company": "Springfield School District",
                        "State": "IL",
                    }
                ],
                "done": True,
            }
        return {"records": [], "done": True}

    monkeypatch.setattr(gateway, "_get", fake_get)
    monkeypatch.setattr(gateway, "lightning_link", campaign_link)
    resolved = gateway.resolve_organizations([("Springfield School District", "IL")])
    people, accounts = resolved["springfield school district|IL"]
    assert len(people) == 1 and accounts == []
    assert "query/next" in paths
