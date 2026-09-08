"""Refresh the reviewed Maine Community Greenhouse school recipients."""

from __future__ import annotations


from ..models import RawItem
from ..reviewed_awards import (
    award_item,
    fetch_document,
    page_text,
    require_text,
    validate_keys,
    section,
)

GREENHOUSE_URL = "https://www3.maine.gov/governor/mills/news/governor-mills-announces-500000-grants-help-expand-local-food-production-and-agricultural"


def parse(payload: bytes) -> list[RawItem]:
    """Bind the three named school entities, leaving unpublished individual sums unknown."""
    article = section(payload, "article")
    text = page_text(str(article).encode())
    require_text(
        text,
        "July 30, 2026",
        "has awarded $500,000 to 10 Maine communities",
        "2026 Community Greenhouse Grant Recipients",
    )
    recipients = (
        ("Limestone Community School", "Limestone"),
        ("MSAD 33–Valley Unified Education Service Center", "St. Agatha"),
        ("Regional School Unit 50", "Dyer Brook"),
    )
    paragraphs = article.select(".news-wrap > p")
    labels = [" ".join(p.get_text(" ", strip=True).split()) for p in paragraphs]
    begin = labels.index("2026 Community Greenhouse Grant Recipients")
    finish = labels.index("About the Program", begin + 1)
    headings = [
        " ".join(p.get_text(" ", strip=True).split())
        for p in paragraphs[begin + 1 : finish]
        if p.find("strong")
    ]
    if len(headings) != 10:
        raise ValueError("Maine greenhouse recipient count changed")
    items = []
    for name, location in recipients:
        if headings.count(f"{name}, {location}") != 1:
            raise ValueError("Maine current recipient missing or duplicated")
        items.append(
            award_item(
                source="me-greenhouse",
                entity=name,
                state="ME",
                entity_type="school",
                program="Community Greenhouse",
                awarded_on="2026-07-30",
                amount=None,
                url=GREENHOUSE_URL,
                evidence="School greenhouse grant announced July 30, 2026. Individual amount unpublished; security-purchase eligibility not established.",
                raw={"location": location},
            )
        )
    return validate_keys(items)


def poll() -> list[RawItem]:
    """Refresh the exact reviewed announcement; drift fails before ingestion."""
    return parse(fetch_document(GREENHOUSE_URL))
