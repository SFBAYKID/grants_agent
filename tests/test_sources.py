"""Parser tests against recorded live fixtures (captured 2026-07-13).

The key regression here is the SVPP filter: the 2026-07-13 first live run proved that
unfiltered CFDA 16.710 is 96% non-school noise (COPS hiring, tribal equipment). These
tests pin that filter, the field mapping of every parser, and WEBS's zero-match day.
"""

from __future__ import annotations

from grant_watch.sources import grants_gov, sam_gov, usaspending, webs


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


# ------------------------------------------------------------------ grants.gov
def test_grants_gov_parses_all_hits(grants_gov_payload) -> None:
    items = grants_gov.parse_opportunities(grants_gov_payload, "school violence prevention")
    assert len(items) == 25
    first = items[0]
    assert first.source == "grants.gov"
    assert first.item_id and first.title
    assert first.url.startswith("https://www.grants.gov/search-results-detail/")
    assert first.raw["matched_keyword"] == "school violence prevention"


# ------------------------------------------------------------------ sam.gov
def test_sam_gov_parses_opportunities(sam_gov_payload) -> None:
    items = sam_gov.parse_opportunities(sam_gov_payload)
    assert len(items) == 4
    for it in items:
        assert it.source == "sam.gov"
        assert it.program == "RFP:sam.gov"
        assert it.item_id  # noticeId present on every record in the fixture


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
