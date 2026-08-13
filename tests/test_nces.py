"""Offline NCES parsing, conservative matching, and lead-enrichment tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grant_watch import db
from grant_watch.enrich import nces
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem


def _districts() -> list[nces.NCESDistrict]:
    """Load the recorded-shape NCES aggregate/location fixture."""
    path = Path(__file__).parent / "fixtures" / "nces_districts.json"
    payload: dict[str, object] = json.loads(path.read_text())
    enrollment = payload["enrollment"]
    location = payload["location"]
    assert isinstance(enrollment, dict) and isinstance(location, dict)
    return nces.parse_districts(enrollment, location)


def test_parse_and_match_unique_district() -> None:
    """USD/Unified naming variants resolve when exactly one normalized LEA exists."""
    districts = _districts()
    match = nces.match_district("Tustin USD", districts)
    assert match is not None
    assert match.nces_id == "0640150"
    assert match.enrollment == 21_220 and match.city == "Tustin"


def test_ambiguous_one_word_district_is_not_matched() -> None:
    """Two Orange LEAs normalize alike, so Grant records no guessed identity."""
    assert nces.match_district("Orange School District", _districts()) is None


def test_exact_nces_detail_parses_the_published_official_website() -> None:
    """The website writer is bound to the requested seven-digit NCES record."""
    html = (
        Path(__file__).parent / "fixtures" / "nces_district_detail.html"
    ).read_text()
    assert (
        nces.parse_official_website(html, "0640150") == "https://www.tustin.k12.ca.us"
    )
    with pytest.raises(ValueError, match="did not confirm"):
        nces.parse_official_website(html, "0640151")
    assert nces._safe_published_website("http://127.0.0.1/admin") == ""


def test_enrich_state_leads_updates_only_unique_matches(tmp_path: Path) -> None:
    """NCES facts attach to Tustin while ambiguous Orange remains unknown."""
    conn = db.connect(tmp_path / "nces.db")
    for item_id, entity in (("1", "Tustin USD"), ("2", "Orange School District")):
        db.upsert_lead(
            conn,
            Lead(
                RawItem(
                    "test",
                    item_id,
                    "award",
                    entity,
                    "CA",
                    "SVPP",
                    1.0,
                    "2026-01-01",
                    "2027-01-01",
                    "",
                    {},
                    event_type=FundingEventType.RECORD_OBSERVED,
                ),
                LeadGrade.GOLD,
                entity_type="school_district",
            ),
        )
    summary = nces.enrich_state_leads(conn, "CA", _districts())
    tustin = conn.execute(
        "SELECT * FROM leads WHERE entity_name='Tustin USD'"
    ).fetchone()
    orange = conn.execute(
        "SELECT * FROM leads WHERE entity_name='Orange School District'"
    ).fetchone()
    assert summary == nces.EnrichmentSummary(2, 1, 1)
    assert tustin["nces_id"] == "0640150" and tustin["enrollment"] == 21_220
    assert orange["nces_id"] is None and orange["enrollment"] is None


def test_enrichment_never_binds_a_same_name_district_from_another_state(
    tmp_path: Path,
) -> None:
    """A requested CA lead cannot inherit an otherwise exact TX district match."""
    conn = db.connect(tmp_path / "wrong-state.db")
    db.upsert_lead(
        conn,
        Lead(
            RawItem(
                "test",
                "1",
                "award",
                "Tustin USD",
                "CA",
                "SVPP",
                1.0,
                "2026-01-01",
                "2027-01-01",
                "",
                {},
                event_type=FundingEventType.RECORD_OBSERVED,
            ),
            LeadGrade.GOLD,
            entity_type="school_district",
        ),
    )
    wrong_state = [
        nces.NCESDistrict("0640150", "Tustin Unified", "TX", "Tustin", 21_220)
    ]

    summary = nces.enrich_state_leads(conn, "CA", wrong_state)

    assert summary.matched == 0
    assert conn.execute("SELECT nces_id FROM leads").fetchone()[0] is None


def test_enrichment_persists_exact_website_provenance_for_rich_actions(
    tmp_path: Path,
) -> None:
    """A bound district gains the exact NCES website evidence the policy consumes."""
    conn = db.connect(tmp_path / "nces-site.db")
    db.upsert_lead(
        conn,
        Lead(
            RawItem(
                "test",
                "1",
                "award",
                "Tustin USD",
                "CA",
                "SVPP",
                1.0,
                "2026-01-01",
                "2027-01-01",
                "",
                {},
                event_type=FundingEventType.RECORD_OBSERVED,
            ),
            LeadGrade.GOLD,
            entity_type="school_district",
        ),
    )

    def website(_nces_id: str) -> nces.NCESWebsiteEvidence:
        """Return the exact-record claim represented by the recorded fixture."""
        return nces.NCESWebsiteEvidence(
            "https://www.tustin.k12.ca.us",
            f"{nces.DISTRICT_DETAIL_URL}?ID2=0640150",
            "verified",
        )

    summary = nces.enrich_state_leads(conn, "CA", _districts(), website_fetcher=website)
    row = conn.execute("SELECT * FROM leads").fetchone()
    assert summary.websites_verified == 1
    assert row["nces_website"] == "https://www.tustin.k12.ca.us"
    assert row["nces_website_status"] == "verified"
    assert row["nces_website_source_url"].endswith("?ID2=0640150")
    assert row["nces_website_checked_at"]
    conn.close()


def test_website_writer_rejects_a_foreign_or_wrong_id_evidence_url(
    tmp_path: Path,
) -> None:
    """A callback cannot relabel a non-NCES page as exact directory evidence."""
    conn = db.connect(tmp_path / "wrong-source.db")
    db.upsert_lead(
        conn,
        Lead(
            RawItem(
                "test",
                "1",
                "award",
                "Tustin USD",
                "CA",
                "SVPP",
                1.0,
                "2026-01-01",
                "2027-01-01",
                "",
                {},
                event_type=FundingEventType.RECORD_OBSERVED,
            ),
            LeadGrade.GOLD,
            entity_type="school_district",
        ),
    )

    def wrong_source(_nces_id: str) -> nces.NCESWebsiteEvidence:
        """Claim a valid-looking site from the wrong exact district record."""
        return nces.NCESWebsiteEvidence(
            "https://www.tustin.k12.ca.us",
            f"{nces.DISTRICT_DETAIL_URL}?ID2=0640151",
            "verified",
        )

    summary = nces.enrich_state_leads(
        conn, "CA", _districts(), website_fetcher=wrong_source
    )
    row = conn.execute("SELECT nces_website,nces_website_status FROM leads").fetchone()
    assert summary.website_unavailable == 1
    assert tuple(row) == (None, "unavailable")


class _Response:
    """Small requests-compatible JSON response for ArcGIS pagination tests."""

    def __init__(self, payload: dict[str, object]) -> None:
        """Initialize the test double."""
        self.payload = payload

    def json(self) -> dict[str, object]:
        """Return the configured ArcGIS payload."""
        return self.payload


def test_fetch_state_pages_both_queries_and_excludes_member_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ArcGIS page is merged and the server query excludes negative MEMBER."""
    calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(nces, "PAGE_SIZE", 1)

    def fake_get(url: str, *, params: dict[str, str]) -> _Response:
        """Return two one-row pages followed by an empty terminal page."""
        calls.append((url, dict(params)))
        offset = int(params["resultOffset"])
        if url == nces.SCHOOL_QUERY_URL:
            rows = [
                {
                    "LEAID": "1",
                    "LEA_NAME": "Alpha District",
                    "LSTATE": "CA",
                    "ENROLLMENT": 100,
                },
                {
                    "LEAID": "2",
                    "LEA_NAME": "Beta District",
                    "LSTATE": "CA",
                    "ENROLLMENT": 200,
                },
            ]
        else:
            rows = [
                {"LEAID": "1", "CITY": "Alpha"},
                {"LEAID": "2", "CITY": "Beta"},
            ]
        features = [{"attributes": rows[offset]}] if offset < len(rows) else []
        return _Response({"features": features})

    monkeypatch.setattr(nces, "polite_get", fake_get)
    districts = nces.fetch_state("CA")
    assert [(item.nces_id, item.enrollment) for item in districts] == [
        ("1", 100),
        ("2", 200),
    ]
    school_calls = [params for url, params in calls if url == nces.SCHOOL_QUERY_URL]
    assert [params["resultOffset"] for params in school_calls] == ["0", "1", "2"]
    assert all("MEMBER>=0" in params["where"] for params in school_calls)


def test_nces_repeated_page_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service that ignores resultOffset cannot silently truncate district data."""
    monkeypatch.setattr(nces, "PAGE_SIZE", 1)

    def repeating(_url: str, *, params: dict[str, str]) -> _Response:
        """Return the identical full page for every requested offset."""
        assert "resultOffset" in params
        return _Response(
            {
                "features": [
                    {
                        "attributes": {
                            "LEAID": "1",
                            "LEA_NAME": "Alpha",
                            "LSTATE": "CA",
                            "ENROLLMENT": 1,
                        }
                    }
                ],
                "exceededTransferLimit": True,
            }
        )

    monkeypatch.setattr(nces, "polite_get", repeating)
    with pytest.raises(ValueError, match="repeated"):
        nces.fetch_state("CA")
