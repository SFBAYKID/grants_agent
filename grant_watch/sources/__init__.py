"""Source registry: one module per data source, each exposing poll() -> list[RawItem].

Verification labels (Constitution rule 1) — status through 2026-09-08:
    reviewed school cohorts verified (six fixed announcements; search-only; see docs/reviewed_school_awards.md)
    usaspending   verified   (live SVPP prime awards + NSGP subaward shape)
    grants.gov    verified   (live opportunities)
    sam.gov       verified   (live run with Chase's key)
    ca_grants     verified   (live CKAN/CSV parse; 831 records in dry-run)
    oregon_buys   unavailable (published PDF moved/withdrawn; runtime disabled)
    webs          parser-tested (live fetch/zero match; positive row needs-testing)
    rfp            needs-testing (verbatim .gov-page RFP extraction; NOT wired — found
                                 ~0 open pages during historical research)
    rfp_aggregator needs-testing (third-party Starbridge fixture parser; NOT wired)

cli.py iterates POLLERS; sam.gov is appended there only when SAM_API_KEY is set, and the
direct RFP probe and third-party aggregator remain research-only until separately reviewed.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models import RawItem
from . import (
    ca_grants,
    grants_gov,
    oregon_buys,
    sam_gov,
    usaspending,
    webs,
    pa_pccd,
    me_greenhouse,
    me_start_time,
    ct_school_awards,
    ny_school_awards,
    nh_school_awards,
)

# (display name, zero-arg poll callable). SAM.gov needs a key -> wired in cli.py.
POLLERS: list[tuple[str, Callable[[], list[RawItem]]]] = [
    ("Grants.gov", grants_gov.poll),
    ("USASpending SVPP", usaspending.poll),
    ("California Grants Portal", ca_grants.poll),
    ("WEBS bid calendar", webs.poll),
    # Search-only refresh of reviewed announcement cohorts, not statewide discovery.
    ("Reviewed PA PCCD awards", pa_pccd.poll),
    ("Reviewed Maine greenhouse awards", me_greenhouse.poll),
    ("Reviewed Maine start-time award", me_start_time.poll),
    ("Reviewed Connecticut school awards", ct_school_awards.poll),
    ("Reviewed New York school awards", ny_school_awards.poll),
    ("Reviewed New Hampshire school awards", nh_school_awards.poll),
]

__all__ = [
    "POLLERS",
    "ca_grants",
    "grants_gov",
    "oregon_buys",
    "sam_gov",
    "usaspending",
    "webs",
]
