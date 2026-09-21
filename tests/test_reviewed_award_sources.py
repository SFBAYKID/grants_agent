"""Reviewed award cohort truth, structural drift, and repeat-ingestion regressions."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pdfplumber

import pytest
from bs4 import BeautifulSoup

from grant_watch import db, scoring
from grant_watch.models import LeadGrade, RawItem
from grant_watch.reviewed_awards import validate_keys
from grant_watch.slack.search import search_leads
from grant_watch.sources import (
    ct_school_awards as ct,
    me_greenhouse as me,
    me_start_time as augusta,
    nh_school_awards as nh,
    ny_school_awards as ny,
    pa_pccd as pa,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reviewed_awards"
PARSERS: list[tuple[Callable[[bytes], list[RawItem]], str, int]] = [
    (pa.parse, "pa_pccd.pdf", 353),
    (me.parse, "me_greenhouse.html", 3),
    (augusta.parse, "me_augusta.html", 1),
    (ct.parse, "ct_special.html", 41),
    (ny.parse, "ny_food.html", 2),
    (nh.parse, "nh_rochester.pdf", 1),
]


@pytest.mark.parametrize(("parse", "filename", "count"), PARSERS)
def test_complete_reviewed_cohorts(
    parse: Callable[[bytes], list[RawItem]], filename: str, count: int
) -> None:
    """Fixtures prove exact cohort parsing, verified dates, and deliberately quiet grading."""
    items = parse((FIXTURES / filename).read_bytes())
    assert len(items) == count
    assert len({i.item_id for i in items}) == count
    for item in items:
        assert "2026-03-08" <= item.event_date <= "2026-09-08"
        assert item.verification_status.value == "verified"
        assert item.backfill
        assert scoring.grade(item, date(2026, 9, 8)).grade == LeadGrade.WATCH


def test_fixture_manifest_hashes() -> None:
    """Retained bytes are independently bound to documented fetch evidence."""
    for row in json.loads((FIXTURES / "manifest.json").read_text()):
        assert (
            hashlib.sha256((FIXTURES / row["fixture"]).read_bytes()).hexdigest()
            == row["fixture_sha256"]
        )


def test_pccd_all_names_counties_and_listed_amounts_match_reviewed_transcription() -> (
    None
):
    """Every multiline row matches the earlier independent PDF transcription."""
    items = pa.parse((FIXTURES / "pa_pccd.pdf").read_bytes())
    with (FIXTURES / "pa_expected.csv").open() as handle:
        expected = list(csv.DictReader(handle))
    for item, row in zip(items, expected, strict=True):
        assert "".join(item.entity.split()) == "".join(row["applicant"].split())
        assert item.raw["county"] == row["county"]
        assert item.raw["iu"] == row["iu"]
        assert item.raw["listed_recommended_amount"] == int(row["amount"])
        assert item.amount is None and item.entity_type == "" and item.end == ""
    duplicated = [
        i for i in items if i.entity.startswith("Indian Creek Valley Christian")
    ]
    assert len(duplicated) == 2 and duplicated[0].item_id != duplicated[1].item_id


def test_ct_conflict_is_preserved_without_manufacturing_amount() -> None:
    """Brooklyn's one-dollar disagreement is source evidence, never silently repaired."""
    items = ct.parse((FIXTURES / "ct_special.html").read_bytes())
    conflicts = [i for i in items if i.raw["amount_conflict"]]
    assert [i.entity for i in conflicts] == ["Brooklyn"]
    assert conflicts[0].amount is None
    assert conflicts[0].raw["reported_total"] == "$146,160"
    assert conflicts[0].raw["programming"] == "$146,159"


def test_nh_reimbursement_never_becomes_a_prospective_spending_window() -> None:
    """Administrative GMS dates retain their purpose outside generic spend fields."""
    item = nh.parse((FIXTURES / "nh_rochester.pdf").read_bytes())[0]
    assert item.amount == 60136.11 and item.event_date == "2026-04-29"
    assert item.start == item.end == ""
    assert item.raw["obligation_deadline"] == "2026-06-30"
    assert "reimbursement" in item.evidence_excerpt


@pytest.mark.parametrize(("parse", "filename", "_count"), PARSERS)
def test_non_document_response_fails_whole_source(
    parse: Callable[[bytes], list[RawItem]], filename: str, _count: int
) -> None:
    """A HTTP-200 error page is not a successful empty award cohort."""
    with pytest.raises(ValueError):
        parse(b"<html><p>Temporarily unavailable</p></html>")


@pytest.mark.parametrize(
    ("parse", "filename", "name"),
    [
        (me.parse, "me_greenhouse.html", "Regional School Unit 50"),
        (ny.parse, "ny_food.html", "KIPP NYC"),
        (augusta.parse, "me_augusta.html", "Augusta Schools"),
    ],
)
def test_archived_mentions_cannot_restore_a_removed_current_recipient(
    parse: Callable[[bytes], list[RawItem]], filename: str, name: str
) -> None:
    """The exact current section must bind recipients, not unrelated page furniture."""
    body = (FIXTURES / filename).read_text().replace(name, "Withdrawn recipient")
    body += f"<aside>Archived mention: {name}; awarded $75,000</aside>"
    with pytest.raises(ValueError):
        parse(body.encode())


def test_augusta_published_date_is_required_even_if_url_has_date() -> None:
    """A URL path cannot substitute for actual announcement-date metadata."""
    body = (
        (FIXTURES / "me_augusta.html")
        .read_bytes()
        .replace(b"2026-06-12T12:00:00", b"2025-06-12T12:00:00")
    )
    with pytest.raises(ValueError, match="publication date"):
        augusta.parse(body)


def test_ct_reorder_does_not_change_identity() -> None:
    """Table sorting cannot create new recipient identities or funding events."""
    body = (FIXTURES / "ct_special.html").read_bytes()
    first = ct.parse(body)
    soup = BeautifulSoup(body, "html.parser")
    table = next(
        t
        for t in soup.find_all("table")
        if "Capital Improvement" in t.get_text(" ", strip=True)
    )
    rows = table.find_all("tr")[1:]
    parent = rows[0].parent
    assert parent is not None
    for row in reversed(rows):
        parent.append(row.extract())
    second = ct.parse(str(soup).encode())
    assert {i.item_id: i.observation_hash() for i in first} == {
        i.item_id: i.observation_hash() for i in second
    }


def test_duplicate_identity_fails_loudly() -> None:
    """No last-row-wins collapse at the ingestion boundary."""
    item = ny.parse((FIXTURES / "ny_food.html").read_bytes())[0]
    with pytest.raises(ValueError, match="duplicate"):
        validate_keys([item, item])


def test_repeated_ingestion_and_changed_amount_keep_one_lead(tmp_path: Path) -> None:
    """Repeat observations are silent; changed source facts append one immutable event."""
    conn = db.connect(tmp_path / "repeat.db")
    item = ct.parse((FIXTURES / "ct_special.html").read_bytes())[0]
    for _ in range(2):
        db.upsert_lead(conn, scoring.grade(item))
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM funding_events").fetchone()[0] == 1
    changed = replace(item, amount=(item.amount or 0) + 100)
    db.upsert_lead(conn, scoring.grade(changed))
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM funding_events").fetchone()[0] == 2


def test_evidenced_school_kinds_survive_into_real_search(tmp_path: Path) -> None:
    """Andover, MSAD, and BOCES must be searchable without inventing expanded names."""
    path = tmp_path / "search.db"
    conn = db.connect(path)
    for parse, filename, _count in PARSERS[1:]:
        for item in parse((FIXTURES / filename).read_bytes()):
            db.upsert_lead(conn, scoring.grade(item))
    for state, count in [("ME", 4), ("CT", 41), ("NY", 2), ("NH", 1)]:
        text, _artifact = search_leads(
            state=state,
            org_type="school",
            record_kind="award",
            date_field="award_received",
            date_from="2026-03-08",
            date_to="2026-09-08",
            db_path=path,
        )
        assert f"Found {count} match" in text


@pytest.mark.parametrize(
    "fact", ["School Year 2024-2025", "(GMS) on April 29, 2026", "by August 14, 2026"]
)
def test_nh_changed_administrative_fact_cannot_keep_old_verified_value(
    monkeypatch: pytest.MonkeyPatch, fact: str
) -> None:
    """Changing one letter fact invalidates its persisted dates or reimbursement period."""
    with pdfplumber.open(FIXTURES / "nh_rochester.pdf") as pdf:
        text = " ".join(
            " ".join((p.extract_text() or "").split()) for p in pdf.pages[37:39]
        )
    assert fact in text
    changed = text.replace(fact, "revised administrative fact")
    fake = MagicMock()
    fake.__enter__.return_value.pages = [
        SimpleNamespace(extract_text=lambda: changed)
    ] * 44
    monkeypatch.setattr(nh.pdfplumber, "open", lambda _payload: fake)
    with pytest.raises(ValueError, match="evidence missing"):
        nh.parse(b"%PDF fixture")
