"""Refresh Rochester's published NH DOE transportation award letter, dated April 29.

The letter is in the district's May 14 board packet, printed PDF pages 38–39.
This is reimbursement for prior transportation costs, not new security purchasing.
"""

from __future__ import annotations

import io

import pdfplumber

from ..models import RawItem
from ..reviewed_awards import award_item, fetch_document, require_text

URL = "https://files-backend.assets.thrillshare.com/documents/asset/uploaded_file/2192/Rsd/f3223740-bbd1-4d6b-aff2-785752a191ec/Full-Board-Agenda-05.14.26.pdf?disposition=inline"


def parse(payload: bytes) -> list[RawItem]:
    """Require the dated state letter, exact recipient, award sum and reimbursement scope."""
    if not payload.startswith(b"%PDF"):
        raise ValueError("NH response is not a PDF")
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        if len(pdf.pages) != 44:
            raise ValueError("NH reviewed board packet page count changed")
        text = " ".join(
            " ".join((p.extract_text() or "").split()) for p in pdf.pages[37:39]
        )
    require_text(
        text,
        "STATE OF NEW HAMPSHIRE",
        "Rochester School District SAU 54",
        "Date: April 29, 2026",
        "has been awarded Transportation Incentive Grant funds in the amount of $60,136.11",
        "reimbursement of the transportation aid differential cost from School Year 2024-2025",
        "funds will be made available in the Grants Management System (GMS) on April 29, 2026",
        "Final reports must be submitted in GMS by August 14, 2026",
        "must be obligated in GMS by June 30, 2026",
    )
    item = award_item(
        source="nh-rochester-transportation",
        entity="Rochester School District SAU 54",
        entity_type="school_district",
        state="NH",
        program="CTE Transportation Incentive",
        awarded_on="2026-04-29",
        amount=60136.11,
        url=URL,
        evidence="NH DOE award letter April 29, 2026; reimbursement of 2024–25 transportation costs. Obligation deadline June 30, 2026; not a security-purchasing grant.",
        raw={
            "source_pages": "38–39",
            "board_packet_date": "2026-05-14",
            "gms_available_on": "2026-04-29",
            "obligation_deadline": "2026-06-30",
            "final_report_due": "2026-08-14",
        },
    )
    item.source_locator = "PDF pages 38–39; NH DOE letter April 29, 2026"
    return [item]


def poll() -> list[RawItem]:
    """Refresh the exact district-published letter without claiming statewide coverage."""
    return parse(fetch_document(URL))
