"""Source registry: one module per data source, each exposing poll() -> list[RawItem].

Verification labels (Constitution rule 1) — status through 2026-07-14:
    usaspending   verified   (live SVPP prime awards + NSGP subaward shape)
    grants.gov    verified   (live opportunities)
    sam.gov       verified   (live run with Chase's key)
    ca_grants     verified   (live CKAN/CSV parse; 831 records in dry-run)
    oregon_buys   partial    (live fetch/table/zero match; positive row needs-testing)
    webs          partial    (live fetch/parser/zero match; positive row needs-testing)

cli.py iterates POLLERS; sam.gov is appended there only when SAM_API_KEY is set.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models import RawItem
from . import ca_grants, grants_gov, oregon_buys, sam_gov, usaspending, webs

# (display name, zero-arg poll callable). SAM.gov needs a key -> wired in cli.py.
POLLERS: list[tuple[str, Callable[[], list[RawItem]]]] = [
    ("Grants.gov", grants_gov.poll),
    ("USASpending SVPP", usaspending.poll),
    ("California Grants Portal", ca_grants.poll),
    ("OregonBuys recent bids", oregon_buys.poll),
    ("WEBS bid calendar", webs.poll),
]

__all__ = ["POLLERS", "ca_grants", "grants_gov", "oregon_buys", "sam_gov",
           "usaspending", "webs"]
