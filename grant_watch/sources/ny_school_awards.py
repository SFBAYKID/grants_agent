"""Refresh New York's reviewed June 2026 school-food infrastructure awards."""

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

URL = "https://agriculture.ny.gov/news/governor-hochul-awards-10-million-through-round-three-new-yorks-regional-school-food"


def parse(payload: bytes) -> list[RawItem]:
    """Bind the two expressly named projects to the source's equal $5 million awards."""
    main = section(payload, "main")
    text = page_text(str(main).encode())
    require_text(
        text,
        "June 12, 2026",
        "$10 million has been awarded to two projects",
        "awarded $5 million each",
        "The projects awarded in round three are:",
    )
    paragraphs = main.find_all("p")
    intro = [
        p
        for p in paragraphs
        if p.get_text().startswith("Governor Kathy Hochul today announced")
    ]
    if len(intro) != 1:
        raise ValueError("NY current award paragraph missing")
    require_text(
        intro[0].get_text(" ", strip=True),
        "$10 million has been awarded to two projects through round three",
        "awarded $5 million each",
    )
    headers = [
        p
        for p in paragraphs
        if p.get_text(strip=True) == "The projects awarded in round three are:"
    ]
    if len(headers) != 1:
        raise ValueError("NY current recipient section missing")
    listing = headers[0].find_next_sibling()
    if listing is None or listing.name != "ul":
        raise ValueError("NY current recipient list missing")
    entries = [
        " ".join(li.get_text(" ", strip=True).split())
        for li in listing.find_all("li", recursive=False)
    ]
    names = ("Putnam-Northern Westchester BOCES", "KIPP NYC")
    if len(entries) != 2 or any(
        sum(e.startswith(f"The {name} project") for e in entries) != 1 for name in names
    ):
        raise ValueError("NY current recipients changed")
    return validate_keys(
        [
            award_item(
                source="ny-school-food",
                entity=name,
                entity_type="school",
                state="NY",
                program="Regional School Food Infrastructure Round 3",
                awarded_on="2026-06-12",
                amount=5000000,
                url=URL,
                evidence="School food infrastructure award announced June 12, 2026. Security-purchase eligibility and payment status not established.",
            )
            for name in names
        ]
    )


def poll() -> list[RawItem]:
    """Refresh the exact reviewed official award announcement."""
    return parse(fetch_document(URL))
