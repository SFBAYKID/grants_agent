"""Refresh Connecticut's reviewed August 2026 special-education grant recipient table."""

from __future__ import annotations

from decimal import Decimal

from bs4 import BeautifulSoup

from ..models import RawItem
from ..reviewed_awards import (
    award_item,
    fetch_document,
    page_text,
    require_text,
    validate_keys,
)

URL = "https://portal.ct.gov/governor/news/press-releases/2026/08-2026/governor-lamont-announces-state-grants-to-strengthen-in-district-special-education"


def _money(value: str) -> Decimal:
    """Parse published table dollars; the source's dash means no component award."""
    return (
        Decimal(value.replace("$", "").replace(",", ""))
        if value not in {"", "—"}
        else Decimal(0)
    )


def parse(payload: bytes) -> list[RawItem]:
    """Keep all 41 source rows, withholding conflicting totals instead of repairing them."""
    text = page_text(payload)
    require_text(
        text,
        "8/17/2026",
        "41 special education programs",
        "local and regional boards of education",
        "The recipients receiving grants",
    )
    tables = [
        t
        for t in BeautifulSoup(payload, "html.parser").find_all("table")
        if "Capital Improvement" in t.get_text(" ", strip=True)
    ]
    if len(tables) != 1:
        raise ValueError("CT recipient table missing or ambiguous")
    items = []
    for row in tables[0].find_all("tr")[1:]:
        cells = [
            " ".join(c.get_text(" ", strip=True).split())
            for c in row.find_all(["td", "th"])
        ]
        if len(cells) != 4 or not cells[0]:
            raise ValueError("CT malformed recipient row")
        name, programming, capital, reported = cells
        total = _money(reported)
        conflict = _money(programming) + _money(capital) != total
        evidence = "Special-education program award announced August 17, 2026. Security-purchase eligibility not established."
        if conflict:
            evidence += f" Source conflict: programming {programming}, capital {capital or 'not stated'}, reported total {reported}; amount withheld."
        items.append(
            award_item(
                source="ct-special-education",
                entity=name,
                entity_type="school",
                state="CT",
                program="High-Quality Special Education Incentive",
                awarded_on="2026-08-17",
                amount=None if conflict else float(total),
                url=URL,
                evidence=evidence,
                raw={
                    "programming": programming,
                    "capital": capital,
                    "reported_total": reported,
                    "amount_conflict": conflict,
                },
            )
        )
    if len(items) != 41:
        raise ValueError("CT recipient count does not reconcile")
    return validate_keys(items)


def poll() -> list[RawItem]:
    """Refresh the reviewed table; new award rounds need independent review."""
    return parse(fetch_document(URL))
