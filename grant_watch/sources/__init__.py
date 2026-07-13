"""Source registry: one module per data source, each exposing poll() -> list[RawItem].

Verification labels (Constitution rule 1) — status of each source as of 2026-07-13:
    usaspending   verified   (live run returned real SVPP awards)
    grants.gov    verified   (live run returned 180 opportunities)
    sam.gov       verified   (live run with Chase's key returned 4 real WA bids)
    webs          needs-testing (fetch+parse ran clean but 0 matches — inconclusive)

cli.py iterates POLLERS; sam.gov is appended there only when SAM_API_KEY is set.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models import RawItem
from . import grants_gov, sam_gov, usaspending, webs

# (display name, zero-arg poll callable). sam_gov needs a key -> wired up in cli.py.
POLLERS: list[tuple[str, Callable[[], list[RawItem]]]] = [
    ("Grants.gov", grants_gov.poll),
    ("USASpending SVPP", usaspending.poll),
    ("WEBS bid calendar", webs.poll),
]

__all__ = ["POLLERS", "grants_gov", "sam_gov", "usaspending", "webs"]
