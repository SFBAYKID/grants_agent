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


@pytest.fixture(autouse=True)
def _never_touch_the_real_database(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Fail any test that opens the developer's actual grant_watch.db.

    `db.connect(db_path=DEFAULT_DB_PATH)` binds its default AT IMPORT TIME, so
    monkeypatching `db.DEFAULT_DB_PATH` does not redirect a bare `db.connect()` —
    it silently keeps writing to the real file. A test doing that migrated this
    developer's database to a new schema and left rows in it before anyone noticed,
    which is exactly the kind of damage a suite is supposed to be incapable of.

    Redirecting the default would hide the mistake; failing loudly makes the test
    pass an explicit path, which is what it should have done.
    """
    from grant_watch import db

    real = Path(db.DEFAULT_DB_PATH).resolve()

    def guarded(db_path: object = None, *args: object, **kwargs: object) -> object:
        """Refuse a connection to the real database, allow any other path."""
        if db_path is None or Path(str(db_path)).resolve() == real:
            raise AssertionError(
                f"{request.node.nodeid} tried to open the real database at {real}. "
                "Pass an explicit tmp_path, or monkeypatch db.connect — patching "
                "db.DEFAULT_DB_PATH does NOT work, because connect() binds its "
                "default at import time."
            )
        return _real_connect(db_path, *args, **kwargs)

    _real_connect = db.connect
    monkeypatch.setattr(db, "connect", guarded)
