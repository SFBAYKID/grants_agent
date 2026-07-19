"""RFP discovery source — trust-bearing pure logic (no Firecrawl/Anthropic).

Per the architectural-critic: the LLM output is UNTRUSTED, so it is just another input
here. Every gate that could mint a fabricated lead is fuzzed on recorded fixtures and
synthetic pages. Live search/scrape/extract are exercised only behind a gated smoke.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from grant_watch import db, scoring
from grant_watch.models import FundingEventType, LeadGrade, RawItem, VerificationStatus
from grant_watch.sources import rfp, rfp_parse

_FIX = Path(__file__).parent / "fixtures" / "rfp"


def _fixture(name: str) -> str:
    """Load recorded scraped markdown."""
    return (_FIX / name).read_text()


# A synthetic OPEN, single-RFP, physical-security page on a government host. The
# pre-bid and questions dates are decoys for the C1 adjacency gate.
OPEN_URL = "https://www.riverton.gov/bids/rfp-2027-11"
OPEN_PAGE = """# City of Riverton — Request for Proposals

RFP 2027-11 Access Control and Video Surveillance System

Category: Request for Proposals (RFP)

The City of Riverton is requesting proposals for a city-wide access control and
video surveillance camera system for municipal facilities.

Pre-bid meeting: July 1, 2027 at 10:00 AM at City Hall.

Questions due July 20, 2027.

Proposals are due August 14, 2027 at 2:00 PM, submitted to the City Clerk,
100 Main Street, Riverton.
"""
OPEN_EXTRACT = {
    "entity": "City of Riverton",
    "state": "",
    "rfp_number": "2027-11",
    "title": "RFP 2027-11 Access Control and Video Surveillance System",
    "due_date": "August 14, 2027",
    "status": "",
    "portal": "",
}
BEFORE_DUE = date(2027, 1, 1)


# --------------------------------------------------------------- happy path (L1/C1/C2)
def test_open_physical_security_rfp_becomes_verified_silver_item() -> None:
    """A single open camera/access-control RFP on a gov host is a VERIFIED RawItem."""
    item = rfp_parse.build_rawitem(OPEN_EXTRACT, OPEN_PAGE, OPEN_URL, BEFORE_DUE)
    assert item is not None
    assert item.source == "rfp"
    assert item.entity == "City of Riverton"
    assert item.end == "2027-08-14"  # the SUBMISSION deadline, in ISO for scoring
    assert item.amount is None  # a solicitation has no awarded dollars
    assert item.program == "RFP:security"
    assert item.event_type is FundingEventType.RFP_POSTED
    assert item.verification_status is VerificationStatus.VERIFIED
    assert "Proposals are due August 14, 2027" in item.evidence_excerpt
    assert item.item_id == "city-of-riverton|2027-11"
    # …and it grades SILVER while open.
    assert scoring.grade(item, today=BEFORE_DUE).grade is LeadGrade.SILVER


# --------------------------------------------------------- C1: label-adjacency gate
def test_pre_bid_meeting_date_is_rejected_even_though_verbatim() -> None:
    """The wrong-but-present date (pre-bid meeting) must not become the deadline."""
    wrong = {**OPEN_EXTRACT, "due_date": "July 1, 2027"}  # the pre-bid date
    assert rfp_parse.build_rawitem(wrong, OPEN_PAGE, OPEN_URL, BEFORE_DUE) is None
    assert rfp_parse.label_adjacent_date(OPEN_PAGE, "July 1, 2027") == ""


def test_questions_due_date_is_rejected() -> None:
    """A 'questions due' date is not the submission deadline."""
    assert rfp_parse.label_adjacent_date(OPEN_PAGE, "July 20, 2027") == ""


def test_hallucinated_date_not_on_page_is_rejected() -> None:
    """A date the model invented (absent from the page) fails the gate."""
    ghost = {**OPEN_EXTRACT, "due_date": "September 9, 2027"}
    assert rfp_parse.build_rawitem(ghost, OPEN_PAGE, OPEN_URL, BEFORE_DUE) is None


# ------------------------------------------------------------------ C2: date parsing
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("August 14, 2027", "2027-08-14"),
        ("May 28, 2026", "2026-05-28"),
        ("Fri, 01/30/2026 - 2:00 PM", "2026-01-30"),
        ("5/28/26", "2026-05-28"),
        ("2026-01-30", "2026-01-30"),
        ("Jan. 5, 2026", "2026-01-05"),
        ("May 1-28, 2026", None),  # a range is ambiguous — omit, never guess
        ("TBD", None),
        ("", None),
        ("02/30/2026", None),  # impossible day
        ("questions by 3pm", None),  # no date token
    ],
)
def test_date_parse_is_exact_or_omitted(raw: str, expected: str | None) -> None:
    """Only a single unambiguous printed date parses to ISO; else None."""
    assert rfp_parse.parse_iso_date(raw) == expected


# ------------------------------------------------------------------ H1: status filter
def test_closed_status_with_future_date_is_dropped() -> None:
    """A future due date does not save an explicitly Closed/Cancelled RFP."""
    closed_page = OPEN_PAGE + "\nStatus: Cancelled\n"
    assert rfp_parse.has_closed_status(closed_page)
    assert rfp_parse.build_rawitem(OPEN_EXTRACT, closed_page, OPEN_URL, BEFORE_DUE) is None


def test_open_is_not_inferred_from_absence_of_status() -> None:
    """No status word present -> the date gate alone decides; not auto-closed."""
    assert not rfp_parse.has_closed_status(OPEN_PAGE)
    assert rfp_parse.build_rawitem(OPEN_EXTRACT, OPEN_PAGE, OPEN_URL, BEFORE_DUE) is not None


def test_past_due_date_is_dropped_even_if_open() -> None:
    """After the deadline it is no longer an open lead."""
    after = date(2027, 12, 1)  # past August 14, 2027
    assert rfp_parse.build_rawitem(OPEN_EXTRACT, OPEN_PAGE, OPEN_URL, after) is None


# ------------------------------------------------------------------ H2: relevance
@pytest.mark.parametrize(
    "text, relevant",
    [
        ("Security Camera System", True),
        ("District-wide door access control", True),
        ("Video surveillance and CCTV upgrade", True),
        ("Security guard services for downtown", False),
        ("Cybersecurity assessment and network security", False),
        ("School Resource Officer (SRO) program", False),
        ("Security deposit refund policy", False),
        ("Information security audit", False),
    ],
)
def test_relevance_allows_physical_blocks_lookalikes(text: str, relevant: bool) -> None:
    """Physical-security only — guard/cyber/SRO/deposit are dropped."""
    assert rfp_parse.is_relevant(text) is relevant


# ------------------------------------------------------------------ C4: entity binding
@pytest.mark.parametrize(
    "entity, url, ok",
    [
        ("City of Kemah", "https://www.kemahtx.gov/bids.aspx?bidID=19", True),
        ("Irvington School District", "https://irvington.k12.nj.us/rfp", True),
        ("City of Woodland", "https://www.ci.woodland.wa.us/police/bids", True),
        ("Johnson Controls", "https://www.kemahtx.gov/bids.aspx?bidID=19", False),
        ("City of Kemah", "https://www.biddingaggregator.com/x", False),  # not gov host
        ("Acme Security Integrators", "https://acme-security.com/rfp", False),
    ],
)
def test_entity_must_be_a_government_echoed_by_its_host(
    entity: str, url: str, ok: bool
) -> None:
    """The awarder is a government on its own official host — never a vendor/aggregator."""
    assert rfp_parse.entity_matches_host(entity, url) is ok


# ------------------------------------------------------------------ C5: multi-RFP pages
def test_multi_rfp_index_page_is_skipped() -> None:
    """A page listing many solicitations is never parsed (cross-row fabrication risk)."""
    index = (
        "Bid Number: 2026-01 Camera RFP proposals due May 1, 2026\n"
        "Bid Number: 2026-02 Access control RFP proposals due June 1, 2026\n"
        "Bid Number: 2026-03 Alarm RFP proposals due July 1, 2026\n"
    )
    assert rfp_parse.is_index_page(index)
    assert rfp_parse.build_rawitem(OPEN_EXTRACT, index, OPEN_URL, BEFORE_DUE) is None


# ------------------------------------------------------------------ C3: dedup / item_id
def test_item_id_is_namespaced_by_entity() -> None:
    """Two cities' '2026-05' never collide; same rfp# on two urls shares one id."""
    a = rfp_parse.rfp_item_id("City of Kemah", "2026-05", "t", "2026-05-28", "u1")
    b = rfp_parse.rfp_item_id("City of Ames", "2026-05", "t", "2026-05-28", "u2")
    assert a != b
    same = rfp_parse.rfp_item_id("City of Kemah", "2026-05", "t2", "2026-06-01", "u9")
    assert a == same  # entity + rfp_number alone key it


def test_item_id_url_fallback_is_normalized() -> None:
    """With no rfp_number/title, a URL differing only by query/fragment/case is one id."""
    x = rfp_parse.rfp_item_id("City of X", "", "", "", "https://X.gov/Bid/1?s=abc")
    y = rfp_parse.rfp_item_id("City of X", "", "", "", "https://x.gov/Bid/1#frag/")
    assert x == y


# ------------------------------------------------------- real fixtures (both are closed)
def test_real_kemah_page_is_dropped_as_closed() -> None:
    """The live City of Kemah page is 'Status: Closed' — never surfaced as open."""
    page = _fixture("kemah_tx_closed.md")
    assert rfp_parse.has_closed_status(page)
    extract = {
        "entity": "City of Kemah",
        "state": "TX",
        "rfp_number": "2026-05",
        "title": "RFP 2026-05 Video Surveillance Camera Systems",
        "due_date": "May 28, 2026",
        "status": "Closed",
        "portal": "",
    }
    # even before the deadline, the explicit Closed status drops it (H1)
    assert rfp_parse.build_rawitem(
        extract, page, "https://www.kemahtx.gov/bids.aspx?bidID=19", date(2026, 1, 1)
    ) is None


def test_real_woodland_page_is_dropped_as_closed() -> None:
    """The live City of Woodland page says 'no longer accepting bids'."""
    page = _fixture("woodland_wa_closed.md")
    assert rfp_parse.has_closed_status(page)


# ------------------------------------------------------------------ scoring
def test_scoring_grades_rfp_source() -> None:
    """source='rfp' grades SILVER on a future ISO due date, WATCH otherwise."""
    assert "rfp" in scoring.RFP_SOURCES
    item = rfp_parse.build_rawitem(OPEN_EXTRACT, OPEN_PAGE, OPEN_URL, BEFORE_DUE)
    assert item is not None
    assert scoring.grade(item, today=BEFORE_DUE).grade is LeadGrade.SILVER
    # a hand-built past-due rfp item is WATCH
    stale = RawItem(
        source="rfp", item_id="e|1", title="Camera RFP", entity="City of X", state="X",
        program="RFP:security", amount=None, start="", end="2020-01-01",
        url="u", event_type=FundingEventType.RFP_POSTED,
    )
    assert scoring.grade(stale, today=BEFORE_DUE).grade is LeadGrade.WATCH


# ---------------------------------------- drip boundary regression (pins Phase-2 gate)
def _rfp_silver_lead(conn: sqlite3.Connection) -> None:
    """Persist one open RFP SILVER lead."""
    item = rfp_parse.build_rawitem(OPEN_EXTRACT, OPEN_PAGE, OPEN_URL, BEFORE_DUE)
    assert item is not None
    # keep the item's future window in play regardless of the calendar the test runs on
    db.upsert_lead(conn, scoring.grade(item, today=BEFORE_DUE))


def test_rfp_silver_lead_never_reaches_drip(tmp_path: Path) -> None:
    """An RFP SILVER lead appears in NEITHER nugget nor bulletin candidates.

    This pins the promise that proactive RFP alerts are a deliberate later phase — it
    fails the instant someone widens the drip queries to include RFP sources."""
    conn = db.connect(tmp_path / "rfp.db")
    _rfp_silver_lead(conn)
    assert db.nugget_candidates(conn) == []
    assert db.bulletin_candidates(conn) == []


# ------------------------------------------------------ poll orchestration (mock I/O)
def test_poll_isolates_a_failing_query_and_still_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One query raising does not sink the run; a good page still yields an item."""

    def fake_search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """First query explodes, the rest return one good URL."""
        if "camera system RFP" in query:
            raise RuntimeError("firecrawl 500")
        return [{"url": OPEN_URL}]

    monkeypatch.setattr(rfp, "_search", fake_search)
    monkeypatch.setattr(rfp, "_scrape", lambda url: OPEN_PAGE)
    monkeypatch.setattr(rfp, "_extract_rfp", lambda page, url: dict(OPEN_EXTRACT))
    items = rfp.poll(today=BEFORE_DUE)
    assert any(i.entity == "City of Riverton" for i in items)
    # deduped across the many queries that all returned the same URL
    assert len({i.item_id for i in items}) == len(items)


def test_poll_raises_when_no_query_reaches_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If EVERY query fails, we raise (never record a false 'no open RFPs')."""

    def always_fail(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Every search throws."""
        raise RuntimeError("network down")

    monkeypatch.setattr(rfp, "_search", always_fail)
    with pytest.raises(rfp.SourceUnreachable):
        rfp.poll()


def test_poll_skips_thin_and_index_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked/short scrape or a multi-RFP index page yields nothing, no extraction."""
    extracted_called = {"n": 0}

    def counting_extract(page: str, url: str) -> dict[str, str]:
        """Fail the test if a thin/index page ever reaches extraction."""
        extracted_called["n"] += 1
        return dict(OPEN_EXTRACT)

    monkeypatch.setattr(rfp, "_search", lambda q, limit=5: [{"url": "https://x.gov/1"}])
    monkeypatch.setattr(rfp, "_scrape", lambda url: "too short")  # < 200 chars
    monkeypatch.setattr(rfp, "_extract_rfp", counting_extract)
    assert rfp.poll(today=BEFORE_DUE) == []
    assert extracted_called["n"] == 0  # never extracted a thin page
