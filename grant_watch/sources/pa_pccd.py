"""Refresh the reviewed June 2026 PCCD conditional school-safety approval cohort.

Verified source document: 2026-09-08. Parser/live acceptance is separate. The
353-row PDF includes 347 schools and six other applicants without row-level kinds.
Listed recommended amounts are evidence, not unconditional awards or cash receipts.
"""

from __future__ import annotations

import io
import re
from typing import TypedDict, cast

import pdfplumber

from ..models import RawItem
from ..reviewed_awards import award_item, fetch_document, require_text, validate_keys

URL = "https://www.pa.gov/content/dam/copapwp-pagov/en/pccd/documents/schoolsafety/school-safety-award-documents/25-26%20targeted%20school%20safety%20final%20awards%20approved%206-3-26.pdf"


class Word(TypedDict):
    """Only the PDF word geometry needed to bind multiline applicant rows."""

    text: str
    x0: float
    top: float
    bottom: float


def _center(word: Word) -> float:
    """Return the vertical center used to match money and IU row anchors."""
    return (word["top"] + word["bottom"]) / 2


def parse(payload: bytes) -> list[RawItem]:
    """Reconcile every row and aggregate before emitting any conditional approvals."""
    if not payload.startswith(b"%PDF"):
        raise ValueError("PCCD response is not a PDF")
    out: list[RawItem] = []
    total = 0
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        if len(pdf.pages) != 11:
            raise ValueError("PCCD reviewed cohort page count changed")
        text = " ".join(" ".join((p.extract_text() or "").split()) for p in pdf.pages)
        require_text(
            text,
            "Project Start Date: July 1, 2026",
            "353 applications",
            "347 nonpublic schools",
            "$19,356,596",
            "pending resolution of any programmatic or fiscal concerns",
        )
        for page_no, page in enumerate(pdf.pages, 1):
            require_text(
                page.extract_text() or "",
                "Approved: School Safety and Security Committee, June 3, 2026",
            )
            if page_no == 1:
                continue
            words = cast(list[Word], page.extract_words())
            # A currency token alone could be the summary total. An aligned IU token
            # in its own column establishes an actual table row before names are read.
            anchors = [
                w
                for w in words
                if 464 <= w["x0"] < 530
                and re.fullmatch(r"\$[\d,]+", w["text"])
                and any(
                    v["text"] == "IU"
                    and 418 <= v["x0"] < 464
                    and abs(_center(v) - _center(w)) < 3
                    for v in words
                )
            ]
            if not anchors:
                raise ValueError("PCCD table page has no validated rows")
            assigned: list[list[Word]] = [[] for _ in anchors]
            for word in words:
                if (
                    not 74 <= word["x0"] < 464
                    or _center(word) < _center(anchors[0]) - 18
                    or word["top"] > 725
                ):
                    continue
                index = min(
                    range(len(anchors)),
                    key=lambda i: abs(_center(anchors[i]) - _center(word)),
                )
                if abs(_center(anchors[index]) - _center(word)) > 24:
                    raise ValueError("PCCD unbound table text")
                assigned[index].append(word)
            for anchor, group in zip(anchors, assigned, strict=True):
                columns = []
                for left, right in ((74, 330), (330, 418), (418, 464)):
                    selected = sorted(
                        (w for w in group if left <= w["x0"] < right),
                        key=lambda w: (round(w["top"] / 3), w["x0"]),
                    )
                    columns.append(" ".join(w["text"] for w in selected))
                name, county, iu = columns
                if not name or not county or not re.fullmatch(r"IU \d{2}", iu):
                    raise ValueError("PCCD malformed applicant row")
                listed = int(anchor["text"].replace("$", "").replace(",", ""))
                total += listed
                evidence = f"Conditional approval June 3, 2026; listed recommended amount ${listed:,}, pending programmatic/fiscal resolution. Applicant category not specified per row; payment and vendor selection unknown."
                item = award_item(
                    source="pa-pccd-conditional",
                    entity=name,
                    state="PA",
                    program="PCCD Targeted School Safety 2025-26",
                    awarded_on="2026-06-03",
                    amount=None,
                    url=URL,
                    identity=f"{county}|{iu}",
                    start="2026-07-01",
                    evidence=evidence,
                    raw={
                        "county": county,
                        "iu": iu,
                        "pdf_page": page_no,
                        "listed_recommended_amount": listed,
                        "approval_conditions": "pending programmatic/fiscal resolution",
                    },
                )
                item.source_locator = f"PDF page {page_no}; {name}; {county}; {iu}"
                out.append(item)
    if len(out) != 353 or total != 19356596:
        raise ValueError("PCCD applicant count or amount total does not reconcile")
    return validate_keys(out)


def poll() -> list[RawItem]:
    """Refresh the exact reviewed official document; changed layouts fail visibly."""
    return parse(fetch_document(URL))
