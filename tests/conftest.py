"""Shared pytest fixtures. All parser tests run on RECORDED live responses in
tests/fixtures/ (captured 2026-07-13) so the suite never hammers government servers.
Live smoke tests are the CLI's job, run manually — never part of this suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def usaspending_16710_wa() -> dict[str, Any]:
    """Real 16.710 WA page 1: 100 rows of COPS-umbrella awards, exactly 4 SVPP."""
    return json.loads((FIXTURES / "usaspending_16710_wa_p1.json").read_text())


@pytest.fixture()
def usaspending_nsgp_wa() -> dict[str, Any]:
    """Real WA NSGP subaward response excerpt captured from the live no-key API."""
    return json.loads((FIXTURES / "usaspending_nsgp_wa.json").read_text())


@pytest.fixture()
def grants_gov_payload() -> dict[str, Any]:
    """Real search2 response for 'school violence prevention' (25 hits)."""
    return json.loads((FIXTURES / "grants_gov_svp.json").read_text())


@pytest.fixture()
def sam_gov_payload() -> dict[str, Any]:
    """Real SAM.gov WA 'security' response (4 opportunities, key scrubbed)."""
    return json.loads((FIXTURES / "sam_gov_wa_security.json").read_text())


@pytest.fixture()
def webs_html() -> str:
    """Real WEBS bid-calendar HTML (187 <tr> rows; verified to contain ZERO security
    keywords on capture day — so 0 parsed items is the CORRECT expectation)."""
    return (FIXTURES / "webs_bidcalendar.html").read_text()


@pytest.fixture()
def ca_grants_opportunities_csv() -> str:
    """Recorded-shape California opportunity rows with physical/cyber/closed cases."""
    return (FIXTURES / "ca_grants_opportunities.csv").read_text()


@pytest.fixture()
def ca_grants_awards_csv() -> str:
    """Recorded-shape California award rows with approved/denied/noise cases."""
    return (FIXTURES / "ca_grants_awards.csv").read_text()
