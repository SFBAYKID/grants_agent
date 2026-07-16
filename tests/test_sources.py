"""Parser tests against recorded live fixtures (captured 2026-07-13).

The key regression here is the SVPP filter: the 2026-07-13 first live run proved that
unfiltered CFDA 16.710 is 96% non-school noise (COPS hiring, tribal equipment). These
tests pin that filter, the field mapping of every parser, and WEBS's zero-match day.
"""

from __future__ import annotations

from datetime import date

import pytest

from grant_watch.sources import (
    ca_grants,
    grants_gov,
    oregon_buys,
    sam_gov,
    usaspending,
    webs,
)


# ------------------------------------------------------------------ usaspending
def test_svpp_filter_drops_cops_umbrella_noise(usaspending_16710_wa) -> None:
    items = usaspending.parse_awards(usaspending_16710_wa, cfda="16.710", state="WA")
    # Fixture verified to hold exactly 4 SVPP rows among 100 umbrella awards.
    assert len(items) == 4
    for it in items:
        assert "SCHOOL VIOLENCE" in it.title.upper() or "SVPP" in it.title.upper()


def test_svpp_16071_rows_pass_unfiltered(usaspending_16710_wa) -> None:
    # 16.071 is SVPP-only by definition: the same payload must NOT be filtered.
    items = usaspending.parse_awards(usaspending_16710_wa, cfda="16.071", state="WA")
    assert len(items) == 100


def test_usaspending_field_mapping(usaspending_16710_wa) -> None:
    item = usaspending.parse_awards(usaspending_16710_wa, "16.710", "WA")[0]
    assert item.source == "usaspending:16.710"  # CFDA suffix is the dedup namespace
    assert item.state == "WA"
    assert item.program == "SVPP"
    assert item.item_id
    assert item.url.startswith("https://www.usaspending.gov/award/")


def test_usaspending_uses_recipient_location_instead_of_query_state() -> None:
    """A same-name district cannot inherit the wrong state from a query loop."""
    payload = {"results": [{
        "Award ID": "2020SVWX0155",
        "Recipient Name": "WASHINGTON COUNTY SCHOOL DISTRICT",
        "Recipient UEI": "MBTAF3NWEMB5",
        "Recipient Location": {
            "state_code": "FL", "city_name": "CHIPLEY",
            "address_line1": "652 THIRD STREET", "zip5": "32428",
        },
        "Award Amount": 500000.0,
        "Start Date": "2020-09-01", "End Date": "2023-08-31",
        "Description": "SVPP",
        "generated_internal_id": "ASST_NON_2020SVWX0155_015",
    }]}
    item = usaspending.parse_awards(payload, "16.710", "UT")[0]
    assert item.state == "FL"
    assert item.raw["Recipient UEI"] == "MBTAF3NWEMB5"
    location = item.raw["Recipient Location"]
    assert isinstance(location, dict) and location["city_name"] == "CHIPLEY"


def test_nsgp_subawards_map_end_recipients_and_explicit_dates(
        usaspending_nsgp_wa) -> None:
    """NSGP subawards expose named end recipients without inventing spend deadlines."""
    items = usaspending.parse_nsgp_subawards(
        usaspending_nsgp_wa, "WA", today=date(2026, 7, 14))
    assert len(items) == 2
    assert items[0].source == "usaspending-subaward:97.008"
    assert items[0].entity == "THE CHABAD JEWISH DISCOVERY CENTER"
    assert items[0].event_date == "2024-11-15"
    assert items[0].end == ""
    assert items[0].backfill is True


def test_watch_states_is_configurable(monkeypatch) -> None:
    """State expansion is configuration, not a code fork."""
    monkeypatch.setenv("GRANT_WATCH_STATES", "ca, OR,ca")
    assert usaspending.watch_states() == ("CA", "OR")


def test_usaspending_poll_follows_pages_and_fails_at_guard(monkeypatch) -> None:
    """Every prime/subaward stream completes or the whole source fails loudly."""
    monkeypatch.setenv("GRANT_WATCH_STATES", "CA")
    calls: list[tuple[str, int, bool]] = []

    def query(cfda: str, _state: str, page: int,
              subawards: bool = False) -> dict[str, object]:
        calls.append((cfda, page, subawards))
        return {"results": [], "page_metadata": {"hasNext": page == 1}}

    monkeypatch.setattr(usaspending, "_query_page", query)
    assert usaspending.poll() == []
    assert all(page in {1, 2} for _cfda, page, _subaward in calls)
    monkeypatch.setattr(usaspending, "MAX_PAGES", 1)
    with pytest.raises(RuntimeError, match="pagination exceeded"):
        usaspending.poll()


# ------------------------------------------------------------------ California portal
def test_ca_opportunities_keep_only_active_physical_security(
        ca_grants_opportunities_csv) -> None:
    """Cyber access controls and closed opportunities do not become physical leads."""
    items = ca_grants.parse_opportunities(
        ca_grants_opportunities_csv, today=date(2026, 7, 14))
    assert len(items) == 1
    assert items[0].item_id == "152757"
    assert items[0].event_date == "2026-07-01"
    assert items[0].application_portal == "https://caloes.example/apply"


def test_ca_csv_mojibake_bom_does_not_hide_portal_id(
        ca_grants_opportunities_csv) -> None:
    """A response decoded with the wrong BOM hint still gets a valid first header."""
    items = ca_grants.parse_opportunities(
        "ï»¿" + ca_grants_opportunities_csv, today=date(2026, 7, 14))
    assert len(items) == 1 and items[0].item_id == "152757"


def test_ca_awards_exclude_denied_and_non_target_camera_projects(
        ca_grants_awards_csv) -> None:
    """Only approved school/city or security-program recipients survive the parser."""
    items = ca_grants.parse_awards(
        ca_grants_awards_csv, "2024-2025", "https://data.ca.gov/source.csv",
        today=date(2026, 7, 14))
    assert [item.entity for item in items] == [
        "Yeshiva Ketana of Los Angeles", "City of Example"]
    assert items[0].event_date == ""  # publish date is not relabeled award date
    assert items[0].backfill is True
    assert items[1].backfill is False
    assert items[1].amount == 400_000.0


# ------------------------------------------------------------------ OregonBuys
def test_oregon_recent_bid_rows_keep_physical_security_only() -> None:
    """Recorded-shape PDF rows retain an open camera bid and reject cyber/noise."""
    rows: list[list[object]] = [
        ["Bid Number", "Procurement\nMethod", "Organization\nName",
         "Bid Opening Date/Time", "Short Description"],
        ["S-P26028-\n00020001", "Competitive Sealed\nBid",
         "Centennial School District", "Aug 7, 2026 2:00:00 PM",
         "Security camera and access control system replacement"],
        ["S-10700-00020002", "Competitive Sealed Proposal",
         "Department of Administrative Services", "Aug 8, 2026 2:00:00 PM",
         "Cyber identity access control software"],
        ["S-29100-00020003", "Competitive Sealed Bid", "Department of Corrections",
         "Aug 9, 2026 2:00:00 PM", "Frozen vegetables"],
    ]
    items = oregon_buys.parse_table_rows(rows, today=date(2026, 7, 14))
    assert len(items) == 1
    assert items[0].item_id == "S-P26028-00020001"
    assert items[0].entity == "Centennial School District"
    assert items[0].end == "2026-08-07"
    assert "docId=S-P26028-00020001" in items[0].url


def test_oregon_malformed_pdf_fails_loudly() -> None:
    """Malformed source bytes never become a successful empty poll."""
    with pytest.raises(Exception):
        oregon_buys.parse_pdf(b"not a PDF")


# ------------------------------------------------------------------ grants.gov
def test_grants_gov_parses_all_hits(grants_gov_payload) -> None:
    items = grants_gov.parse_opportunities(grants_gov_payload, "school violence prevention")
    assert len(items) == 25
    first = items[0]
    assert first.source == "grants.gov"
    assert first.item_id and first.title
    assert first.url.startswith("https://www.grants.gov/search-results-detail/")
    assert first.raw["matched_keyword"] == "school violence prevention"


def test_grants_gov_poll_paginates_and_deduplicates(monkeypatch) -> None:
    """Official hitCount/startRecordNum pagination cannot truncate phrase results."""
    monkeypatch.setattr(grants_gov, "KEYWORDS", ("physical security",))
    seen: list[int] = []

    class Response:
        """Small response wrapper for an official search2-shaped payload."""

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def json(self) -> dict[str, object]:
            """Return the recorded page payload."""
            return self.payload

    def post(_url: str, body: dict[str, object]) -> Response:
        start = int(body["startRecordNum"])
        seen.append(start)
        ids = ["1", "2"] if start == 0 else ["2", "3"]
        hits = [{"id": item, "title": f"Grant {item}", "agency": "DOJ",
                 "openDate": "07/01/2026", "closeDate": "08/01/2026"}
                for item in ids]
        return Response({"data": {"hitCount": 4, "oppHits": hits}})

    monkeypatch.setattr(grants_gov, "polite_post", post)
    items = grants_gov.poll()
    assert seen == [0, 2]
    assert [item.item_id for item in items] == ["1", "2", "3"]


# ------------------------------------------------------------------ sam.gov
def test_sam_gov_parses_opportunities(sam_gov_payload) -> None:
    items = sam_gov.parse_opportunities(sam_gov_payload)
    assert len(items) == 4
    for it in items:
        assert it.source == "sam.gov"
        assert it.program == "RFP:sam.gov"
        assert it.item_id  # noticeId present on every record in the fixture


def test_sam_poll_uses_total_records_and_page_offsets(monkeypatch) -> None:
    """SAM's official offset pages are exhausted rather than silently capped."""
    seen: list[int] = []

    class Response:
        """Small response wrapper for a SAM opportunity page."""

        def __init__(self, offset: int) -> None:
            self.offset = offset

        def json(self) -> dict[str, object]:
            """Return one distinct notice on each of two pages."""
            return {
                "totalRecords": 2,
                "opportunitiesData": [{
                    "noticeId": f"N{self.offset}", "title": "Security cameras",
                    "fullParentPathName": "Agency", "postedDate": "2026-07-01",
                    "responseDeadLine": "2026-08-01", "uiLink": "https://sam.gov/opp",
                }],
            }

    def get(_url: str, params: dict[str, object]) -> Response:
        offset = int(params["offset"])
        seen.append(offset)
        return Response(offset)

    monkeypatch.setattr(sam_gov, "polite_get", get)
    items = sam_gov.poll("test-key")
    assert seen == [0, 1]
    assert [item.item_id for item in items] == ["N0", "N1"]


# ------------------------------------------------------------------ webs
def test_webs_zero_matches_on_keywordless_day(webs_html) -> None:
    """Capture-day page verifiably contains no security keywords anywhere in the raw
    HTML — so zero items is CORRECT, and anything more would be a false positive."""
    assert webs.parse_bid_calendar(webs_html) == []


def test_webs_extracts_a_security_row() -> None:
    """Synthetic ASP.NET-style row: proves keyword match + Ref# extraction work when
    a security bid does appear. (Entity extraction is deferred until a real security
    bid provides a fixture — see module docstring.)"""
    html = """<table><tr><td>Group Header: CITY OF OLYMPIA</td></tr>
    <tr><td>Ref #: 2026-123</td><td>Security camera replacement, city hall</td>
    <td>07/30/2026</td></tr></table>"""
    items = webs.parse_bid_calendar(html)
    assert len(items) == 1
    assert items[0].item_id == "2026-123"
    assert items[0].raw["matched_keyword"].lower() == "security"
