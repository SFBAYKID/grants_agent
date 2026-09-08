"""Search-only import boundaries and cross-consumer conditional award truth."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import requests

from grant_watch import db, db_engagement, persequor_client, reviewed_awards, scoring
from grant_watch.campaign import delivery, preparation
from grant_watch.enrich import salesforce_contact_records as sf
from grant_watch.record_semantics import semantics_for
from grant_watch.slack import daily_list, nudge_sources, persequor, search_presentation
from grant_watch.sources import (
    ny_school_awards as ny,
    pa_pccd as pa,
)
from tests.drip_support import mk_lead
from tests.test_reviewed_award_sources import FIXTURES
from tests.test_rich_preparation import _eligible_conn


def test_grade_independent_proactive_and_paid_preparation_exclusion(
    tmp_path: Path,
) -> None:
    """Otherwise eligible GOLD controls survive; reviewed sources never enter push pools."""
    conn = db.connect(tmp_path / "quiet.db")
    quiet = mk_lead(
        conn,
        iid="quiet",
        source=reviewed_awards.QUIET_SOURCE_PREFIX + "ct-special-education",
        start="2026-08-17",
        backfill=True,
    )
    normal = mk_lead(conn, iid="control", start="2026-08-17", backfill=True)
    pools = [
        db_engagement.nugget_candidates(conn, "C"),
        preparation._rows(conn, "C", 1, date(2026, 9, 8)),
        daily_list.candidates(conn, "C", 1, date(2026, 9, 8)),
    ]
    for rows in pools:
        assert [r["id"] for r in rows] == [normal]
        assert quiet not in {r["id"] for r in rows}
    db.record_post(conn, "nugget", quiet, "C", "quiet.1", "gold")
    db.record_post(conn, "nugget", normal, "C", "normal.1", "gold")
    conn.execute("UPDATE posts SET posted_at='2026-09-01T18:00:00+00:00'")
    conn.commit()
    nudges = nudge_sources._unengaged_cards(
        conn, datetime(2026, 9, 8, 18, tzinfo=timezone.utc)
    )
    assert nudges
    assert all(n.observed["lead_id"] != quiet for n in nudges)


def test_rich_delivery_rechecks_source_after_snapshot(tmp_path: Path) -> None:
    """A prepared card is vetoed even if its source changes after selection."""
    conn = _eligible_conn(tmp_path / "veto.db")
    now = datetime(2026, 7, 22, 18, tzinfo=timezone.utc)
    assert delivery._delivery_veto(conn, 1, 2, "contact-1", now)
    conn.execute(
        "UPDATE leads SET source=? WHERE id=1", (reviewed_awards.CONDITIONAL_SOURCE,)
    )
    assert not delivery._delivery_veto(conn, 1, 2, "contact-1", now)


def test_conditional_approval_is_honest_across_search_export_crm_and_outreach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conditional status survives every central consumer even if an amount is filled."""
    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "chase@monarchconnected.com")
    conn = db.connect(tmp_path / "conditional.db")
    item = pa.parse((FIXTURES / "pa_pccd.pdf").read_bytes())[0]
    db.upsert_lead(conn, scoring.grade(item))
    conn.execute("UPDATE leads SET amount=75000,lead_grade='gold'")
    row = db.get_lead(conn, 1)
    assert row is not None
    meaning = semantics_for(row)
    assert not meaning.asserts_award and not meaning.asserts_amount
    assert "conditional approval" in search_presentation.window_label(row)
    assert "conditionally approved" in search_presentation.entity_role_for_row(row)
    assert "money to spend" not in str(
        search_presentation.grade_phrases("award", [row])
    )
    assert "conditional" in sf.grant_summary(
        row
    ) and "75,000" not in sf._grant_headline(row)
    draft = persequor.compose_draft(row)
    assert "conditional grant approval" in draft and "75,000" not in draft
    brief = persequor_client.build_brief(
        row, None, "U01DPJVURHU", "chase@monarchconnected.com"
    )
    assert brief is not None
    assert "conditional" in brief["angle"]
    assert brief["amount_usd"] is None and brief["window_start"] is None
    assert brief["window_end"] is None and brief["expires_at"] is None


def test_empty_entity_type_preserves_prechange_observation_hash() -> None:
    """Pinned hash was calculated with the pre-change models.py, not copied logic."""
    item = ny.parse((FIXTURES / "ny_food.html").read_bytes())[0]
    legacy = replace(item, entity_type="")
    assert (
        legacy.observation_hash()
        == "2c726eb818fbe6f4048225679a2e58d0dc7d346a7caa0d5f2d117c1fa013d0f5"
    )
    assert item.observation_hash() != legacy.observation_hash()


class Response:
    """Minimal streaming HTTP response with explicit status and bytes."""

    def __init__(self, status: int, body: bytes) -> None:
        """Configure the fake without any network access."""
        self.status_code = status
        self.body = body

    def __enter__(self) -> Response:
        """Enter the same response lifetime as requests."""
        return self

    def __exit__(self, *_args: object) -> None:
        """No external resources exist in this fake."""

    def raise_for_status(self) -> None:
        """Expose HTTP failures before bytes can become source facts."""
        if self.status_code >= 400:
            raise requests.HTTPError("fixture server failure")

    def iter_content(self, _chunk_size: int) -> Iterator[bytes]:
        """Emit the configured response body exactly once."""
        yield self.body


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (302, b"", ValueError),
        (500, b"error", requests.HTTPError),
        (200, b"12345", ValueError),
        (200, b"ok", None),
    ],
)
def test_fetch_is_bounded_and_refuses_redirects(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    error: type[Exception] | None,
) -> None:
    """Never follow a moved source, accept a HTTP error, or consume unbounded bytes."""

    def get(_url: str, **kwargs: object) -> Response:
        """Assert redirects stay disabled at the HTTP call itself."""
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        return Response(status, body)

    monkeypatch.setattr(reviewed_awards.requests, "get", get)
    monkeypatch.setattr(reviewed_awards.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(reviewed_awards, "MAX_BYTES", 4)
    if error:
        with pytest.raises(error):
            reviewed_awards.fetch_document(ny.URL)
    else:
        assert reviewed_awards.fetch_document(ny.URL) == b"ok"
