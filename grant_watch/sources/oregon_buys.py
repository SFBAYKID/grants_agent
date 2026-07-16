"""OregonBuys official recent-bids PDF watcher for physical-security solicitations.

Why: OregonBuys' full search requires a supplier session, but Oregon DAS publishes a
public, no-key PDF of selected bids posted during the past seven days. The table has
stable bid number, organization, opening deadline, and description columns plus links.
Grant does not automate around the supplier-login boundary.

Verification: fetch, text extraction, table extraction, and zero security matches were
verified live 2026-07-14. Entity/keyword parsing remains needs-testing until the live PDF
contains a real physical-security bid; synthetic recorded-shape tests cover that branch.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

import pdfplumber

from ..models import (
    DatePrecision,
    FundingEventType,
    RawItem,
    VerificationStatus,
)
from .base import polite_get

PDF_URL = "https://www.oregon.gov/das/ORBuys/Documents/Recent-Bids.pdf"
DETAIL_URL = "https://oregonbuys.gov/bso/external/bidDetail.sda"
_PHYSICAL_RE = re.compile(
    r"security camera|video surveillance|physical security|access control system"
    r"|door hardening|panic alarm|intrusion detection|perimeter security"
    r"|visitor management|CCTV",
    re.IGNORECASE,
)
_CYBER_RE = re.compile(
    r"cyber|identity|multi-factor|single sign-on|software|application licensing",
    re.IGNORECASE,
)


def _clean(value: object) -> str:
    """Collapse PDF cell line breaks and repeated whitespace."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _deadline(raw: str) -> str:
    """Normalize the Oregon table's opening timestamp to an ISO date."""
    try:
        return (
            datetime.strptime(raw.strip(), "%b %d, %Y %I:%M:%S %p").date().isoformat()
        )
    except ValueError:
        return ""


def parse_table_rows(
    rows: list[list[object]], today: date | None = None
) -> list[RawItem]:
    """Parse one extracted PDF table, keeping only open physical-security bids."""
    today = today or date.today()
    out: list[RawItem] = []
    for cells in rows:
        if len(cells) < 5 or _clean(cells[0]).lower() == "bid number":
            continue
        bid_id = re.sub(r"\s+", "", _clean(cells[0]))
        method = _clean(cells[1])
        entity = _clean(cells[2])
        deadline = _deadline(_clean(cells[3]))
        description = _clean(cells[4])
        if not bid_id or not entity or not deadline or deadline < today.isoformat():
            continue
        if not _PHYSICAL_RE.search(description) or _CYBER_RE.search(description):
            continue
        detail_url = f"{DETAIL_URL}?docId={bid_id}&external=true&parentUrl=close"
        out.append(
            RawItem(
                source="oregonbuys",
                item_id=bid_id,
                title=description[:300],
                entity=entity,
                state="OR",
                program="RFP:OregonBuys",
                amount=None,
                start="",  # PDF promises only 'posted in past 7 days', not an exact date
                end=deadline,
                url=detail_url,
                raw={"procurement_method": method, "pdf_url": PDF_URL},
                event_type=FundingEventType.RFP_POSTED,
                event_date="",
                date_precision=DatePrecision.UNKNOWN,
                funded_scope=description[:500],
                source_locator=f"Recent-Bids.pdf bid {bid_id}",
                evidence_excerpt=description[:500],
                verification_status=VerificationStatus.NEEDS_TESTING,
            )
        )
    return out


def parse_pdf(pdf_bytes: bytes, today: date | None = None) -> list[RawItem]:
    """Extract every table from an Oregon recent-bids PDF; malformed PDFs fail loudly."""
    out: list[RawItem] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as document:
        for page in document.pages:
            table = page.extract_table()
            if table:
                out.extend(parse_table_rows(table, today=today))
    return out


def poll() -> list[RawItem]:
    """Fetch and parse Oregon DAS's public seven-day selected-bids publication."""
    return parse_pdf(polite_get(PDF_URL).content)
