"""Shared fixture factories for the drip-engine tests.

Extracted from test_drip.py when `ruff format` pushed it past the 1000-line cap
(CLAUDE.md rule 4). Both test_drip.py and test_drip_builders.py build leads through
these, so the two files cannot drift into disagreeing about what a lead looks like.
"""

from __future__ import annotations

import sqlite3

from grant_watch import db
from grant_watch.models import (
    DatePrecision,
    FundingEventType,
    Lead,
    LeadGrade,
    RawItem,
    VerificationStatus,
)


def mk_lead(
    conn: sqlite3.Connection,
    iid: str = "A1",
    entity: str = "Castle Rock School District 401",
    grade: LeadGrade = LeadGrade.GOLD,
    source: str = "usaspending:16.071",
    amount: float | None = 500_000.0,
    start: str = "2025-10-01",
    end: str = "2028-09-30",
    title: str = "SVPP award",
    backfill: bool = False,
) -> int:
    """Provide test-local behavior for mk lead.

    `backfill` reproduces what every award poller actually sets for an award obligated
    more than 90 days ago — the shape ALL 638 production gold leads have — which
    db.upsert_lead stores as suppressed=1."""
    event_type = (
        FundingEventType.APPLICATION_WINDOW_OPENED
        if source in {"grants.gov", "ca-grants-portal"}
        else FundingEventType.AWARD_OBLIGATED
    )
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source=source,
                item_id=iid,
                title=title,
                entity=entity,
                state="WA",
                program="SVPP",
                amount=amount,
                start=start,
                end=end,
                url="https://x.gov/a",
                raw={},
                event_type=event_type,
                event_date=start,
                date_precision=DatePrecision.DAY,
                verification_status=VerificationStatus.VERIFIED,
                backfill=backfill,
            ),
            grade=grade,
        ),
    )
    return int(
        conn.execute("SELECT id FROM leads WHERE source_item_id=?", (iid,)).fetchone()[
            "id"
        ]
    )


def mk_rfp(
    conn: sqlite3.Connection,
    iid: str = "R1",
    entity: str = "City of Kemah",
    grade: LeadGrade = LeadGrade.SILVER,  # RFPs are silver at best (never gold)
    end: str = "2030-12-31",
    title: str = "Video Surveillance Camera Systems RFP",
    url: str = "https://www.kemahtx.gov/bids",
) -> int:
    """Insert one official SAM physical-security RFP lead."""
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="sam.gov",
                item_id=iid,
                title=title,
                entity=entity,
                state="TX",
                program="RFP:security",
                amount=None,
                start="2030-01-01",
                end=end,
                url=url,
                raw={},
                event_type=FundingEventType.RFP_POSTED,
                event_date="2030-01-01",
                date_precision=DatePrecision.DAY,
                verification_status=VerificationStatus.VERIFIED,
                evidence_excerpt=f"Proposals due 2030-12-31 — {title}",
            ),
            grade=grade,
        ),
    )
    return int(
        conn.execute("SELECT id FROM leads WHERE source_item_id=?", (iid,)).fetchone()[
            "id"
        ]
    )


class SlackClient:
    """Offline Slack client that records successful proactive delivery attempts."""

    def __init__(self, fail: bool = False) -> None:
        """Initialize the test double."""
        self.fail = fail
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:  # noqa: N802
        """Return a stable timestamp or simulate an ambiguous timeout."""
        self.calls += 1
        self.last_kwargs = kwargs
        if self.fail:
            raise TimeoutError("ambiguous")
        return {"ts": "200.1"}
