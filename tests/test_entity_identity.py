"""Human-confirmed identities lift a name/state collision for exactly that key.

On 2026-09-22 the collision rule left 26 of 79 Oregon organizations out of a
rep's campaign; 25 were one organization with several NSGP subawards. Offline.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import db, entity_identity
from grant_watch.enrich.salesforce_campaign_batch import _group_rows
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem


def _write(path: Path, entries: object) -> Path:
    """Write one confirmations file."""
    path.write_text(json.dumps({"confirmations": entries}), encoding="utf-8")
    return path


def _entry(key: str, **overrides: str) -> dict[str, str]:
    """One fully attributed confirmation."""
    return {
        "canonical_entity_key": key,
        "confirmed_by": "Chase",
        "confirmed_on": "2026-09-22",
        **overrides,
    }


def test_only_attributed_entries_are_trusted(tmp_path: Path) -> None:
    """An identity claim with no author or date is exactly what must be refused."""
    path = _write(
        tmp_path / "c.json",
        [
            _entry("congregation beth israel|OR"),
            _entry("no author|OR", confirmed_by=""),
            _entry("no date|OR", confirmed_on=""),
            _entry("no state"),
            "not a dict",
        ],
    )
    assert entity_identity.confirmed_single_organizations(path) == {
        "congregation beth israel|OR"
    }


@pytest.mark.parametrize("content", ["{not json", '{"confirmations": 3}', "[]"])
def test_a_malformed_file_confirms_nothing(tmp_path: Path, content: str) -> None:
    """Fail closed: a broken file must never widen what counts as one org."""
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")
    assert entity_identity.confirmed_single_organizations(path) == frozenset()
    assert entity_identity.confirmed_single_organizations(tmp_path / "absent") == (
        frozenset()
    )


def _rows(conn: sqlite3.Connection, entity: str, count: int) -> None:
    """Insert `count` NSGP subaward rows for one name in Oregon."""
    for index in range(count):
        db.upsert_lead(
            conn,
            Lead(
                item=RawItem(
                    source="usaspending-subaward:97.008",
                    item_id=f"{entity}-{index}",
                    title="NSGP subaward",
                    entity=entity,
                    state="OR",
                    program="NSGP",
                    amount=150_000,
                    start="",
                    end="",
                    url=f"https://source.test/{entity}/{index}",
                    raw={},
                    event_type=FundingEventType.AWARD_OBLIGATED,
                ),
                grade=LeadGrade.WATCH,
            ),
        )


def test_a_confirmed_key_groups_as_one_and_an_unconfirmed_one_still_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confirmation is scoped to its key; Chabad-style names stay excluded."""
    conn = db.connect(tmp_path / "t.db")
    _rows(conn, "CONGREGATION BETH ISRAEL", 3)
    _rows(conn, "CHABAD CENTER FOR JEWISH LIFE", 2)
    key = db.canonical_entity_key("CONGREGATION BETH ISRAEL", "OR")
    monkeypatch.setattr(
        entity_identity,
        "CONFIRMATIONS_PATH",
        _write(tmp_path / "c.json", [_entry(key)]),
    )
    rows = list(conn.execute("SELECT * FROM leads ORDER BY id"))
    groups = {g["entity_name"]: g for g in _group_rows(rows)}
    beth = groups["CONGREGATION BETH ISRAEL"]
    assert beth["identity_collision"] is False
    assert len(beth["source_lead_ids"]) == 3
    assert groups["CHABAD CENTER FOR JEWISH LIFE"]["identity_collision"] is True
