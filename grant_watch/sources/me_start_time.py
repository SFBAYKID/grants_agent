"""Refresh the reviewed Maine DOE Augusta planning-grant announcement."""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..models import RawItem
from ..reviewed_awards import (
    award_item,
    fetch_document,
    page_text,
    require_text,
    section,
)

AUGUSTA_URL = "https://mainedoenews.net/2026/06/12/augusta-schools-receives-later-secondary-school-start-time-planning-grant/"


def parse(payload: bytes) -> list[RawItem]:
    """Bind Augusta's named planning award and its explicitly published contract window."""
    article = section(payload, "main article .entry-content")
    text = page_text(str(article).encode())
    first = article.find("p")
    require_text(
        first.get_text(" ", strip=True) if first else "",
        "award of a one-time, competitive Later Secondary School Start Time Planning Grant to",
        "Augusta Schools",
        "$75,000",
    )
    published = BeautifulSoup(payload, "html.parser").find(
        "meta", property="article:published_time"
    )
    if published is None or not str(published.get("content", "")).startswith(
        "2026-06-12T"
    ):
        raise ValueError("Augusta publication date changed or missing")
    require_text(
        text,
        "award of a one-time, competitive Later Secondary School Start Time Planning Grant to Augusta Schools",
        "$75,000",
        "June 1 to September 10, 2026",
    )
    return [
        award_item(
            source="me-start-time",
            entity="Augusta Schools",
            state="ME",
            entity_type="school_district",
            program="Later Secondary School Start Time Planning",
            awarded_on="2026-06-12",
            amount=75000,
            start="2026-06-01",
            end="2026-09-10",
            url=AUGUSTA_URL,
            evidence="Award for later secondary school start time planning. Contract June 1–September 10, 2026; security-purchase eligibility not established.",
        )
    ]


def poll() -> list[RawItem]:
    """Refresh the exact reviewed announcement; drift fails before ingestion."""
    return parse(fetch_document(AUGUSTA_URL))
