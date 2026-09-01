"""A rep saying "I'm taking this one", and every way that can go wrong.

The trigger was live: a rep claimed Gobles Public Schools in Slack, Grant had nothing
to record it on, and the follow-up system would have chased him about the same lead
and then reported his silence to his manager. These tests pin the two properties that
prevents — a claim is recorded with the words it came from, and it can never be
guessed onto the wrong organization.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grant_watch import db, lead_claims

FAR_FUTURE = datetime(2031, 5, 4, 9, 30, tzinfo=timezone.utc)


def _seed(tmp_path: Path) -> sqlite3.Connection:
    """A database holding the real Gobles shape: one org duplicated, one look-alike.

    Lead 1 and 2 are the SAME Michigan district stored twice with different casing —
    the duplication Salesforce showed and the laptop database confirms. Lead 3 is a
    different organization in Maine that shares the word "Gobles"; lead 4 is dead and
    must never resolve.
    """
    conn = db.connect(tmp_path / "claims.db")
    rows = [
        (1, "GOBLES PUBLIC SCHOOLS", "MI", "new"),
        (2, "Gobles Public Schools", "MI", "new"),
        (3, "GOBLES ELEMENTARY", "ME", "new"),
        (4, "GOBLES CHARTER ACADEMY", "MI", "dead"),
    ]
    for lead_id, name, state, status in rows:
        conn.execute(
            """INSERT INTO leads
                 (id,source,source_item_id,lead_grade,entity_name,state,
                  canonical_entity_key,status)
               VALUES (?,?,?,'gold',?,?,?,?)""",
            (
                lead_id,
                "test",
                f"item-{lead_id}",
                name,
                state,
                db.canonical_entity_key(name, state),
                status,
            ),
        )
    conn.commit()
    return conn


def _claim(conn: sqlite3.Connection, org: lead_claims.Organization, who: str) -> None:
    """Record one claim with the boilerplate the tests do not care about."""
    lead_claims.claim(
        conn,
        org,
        slack_user=who,
        audience="C0BSDPM2KPB",
        thread_ts="1756742520.000100",
        message_ts="1756742520.000100",
        claim_text="I'm taking Gobles Public Schools",
        now=FAR_FUTURE,
    )


def _michigan(conn: sqlite3.Connection) -> lead_claims.Organization:
    """The Michigan district, resolved the way the tool resolves it."""
    return next(org for org in lead_claims.resolve(conn, "gobles") if org.state == "MI")


def test_duplicate_rows_of_one_organization_resolve_as_one(tmp_path: Path) -> None:
    """Two spellings of one district are ONE thing to claim, not two.

    Claiming one row of a duplicated organization would leave the other free to be
    carded — the false-negative shape this repo keeps finding.
    """
    conn = _seed(tmp_path)
    michigan = _michigan(conn)
    assert michigan.lead_ids == (1, 2)


def test_same_name_different_state_stays_two_organizations(tmp_path: Path) -> None:
    """Gobles MI and Gobles ME must never collapse — Salesforce really holds both.

    The CONTROL for the grouping above: if the canonical key ignored state, this
    would be one organization and a claim on Michigan would silence Maine.
    """
    conn = _seed(tmp_path)
    found = lead_claims.resolve(conn, "gobles")
    assert sorted(org.state for org in found) == ["ME", "MI"]


def test_a_dead_lead_never_resolves(tmp_path: Path) -> None:
    """`SEARCHABLE_LEAD_PREDICATE` is honored, so a dead lead cannot be claimed."""
    conn = _seed(tmp_path)
    assert all(4 not in org.lead_ids for org in lead_claims.resolve(conn, "gobles"))


def test_an_empty_canonical_key_is_recomputed_not_faked(tmp_path: Path) -> None:
    """A blank stored key regroups by the real function, never by the raw name.

    `org_backfill` records what the raw-name fallback cost: a value that can never
    equal a stored canonical key, which split one organization in two.
    """
    conn = _seed(tmp_path)
    conn.execute("UPDATE leads SET canonical_entity_key='' WHERE id=2")
    conn.commit()
    assert _michigan(conn).lead_ids == (1, 2)


def test_like_metacharacters_are_literal(tmp_path: Path) -> None:
    """A name containing % must not match every organization on file."""
    conn = _seed(tmp_path)
    assert lead_claims.resolve(conn, "%") == []


def test_a_claim_keeps_the_words_it_came_from(tmp_path: Path) -> None:
    """The receipt is the point: Grant later quotes this to a third party."""
    conn = _seed(tmp_path)
    _claim(conn, _michigan(conn), "U_KERRY")
    held = lead_claims.live_claims(conn, (1, 2))
    assert held[1].claim_text == "I'm taking Gobles Public Schools"
    assert held[1].slack_user == "U_KERRY"
    assert held[1].claimed_at.startswith("2031-05-04T09:30")


def test_reclaiming_your_own_lead_files_nothing_new(tmp_path: Path) -> None:
    """Saying it twice is not two claims — and reports honestly which it was."""
    conn = _seed(tmp_path)
    michigan = _michigan(conn)
    assert lead_claims.claim(
        conn,
        michigan,
        slack_user="U_KERRY",
        audience="C1",
        thread_ts="1.0",
        message_ts="1.0",
        claim_text="mine",
        now=FAR_FUTURE,
    ) == (2, 0)
    assert lead_claims.claim(
        conn,
        michigan,
        slack_user="U_KERRY",
        audience="C1",
        thread_ts="1.0",
        message_ts="1.0",
        claim_text="mine",
        now=FAR_FUTURE,
    ) == (0, 2)


def test_another_rep_is_refused_and_the_holder_is_named(tmp_path: Path) -> None:
    """Never a silent transfer: the refusal carries who has it and what they said."""
    conn = _seed(tmp_path)
    _claim(conn, _michigan(conn), "U_KERRY")
    with pytest.raises(lead_claims.AlreadyClaimed) as raised:
        _claim(conn, _michigan(conn), "U_NELLY")
    assert raised.value.held_by.slack_user == "U_KERRY"
    assert raised.value.held_by.claim_text == "I'm taking Gobles Public Schools"


def test_a_refused_claim_writes_absolutely_nothing(tmp_path: Path) -> None:
    """Half an organization owned by each of two reps is a state nothing can describe.

    Lead 1 is held; lead 2 is free. A second rep claiming the organization must not
    quietly take the free half — this asserts on the LEDGER, not on the return value.
    """
    conn = _seed(tmp_path)
    conn.execute(
        """INSERT INTO lead_claims
             (lead_id,slack_user,audience,thread_ts,message_ts,claim_text,claimed_at)
           VALUES (1,'U_KERRY','C1','1.0','1.0','mine','2031-01-01T00:00:00+00:00')"""
    )
    conn.commit()
    with pytest.raises(lead_claims.AlreadyClaimed):
        _claim(conn, _michigan(conn), "U_NELLY")
    assert lead_claims.live_claims(conn, (2,)) == {}


def test_the_schema_itself_refuses_a_second_live_claim(tmp_path: Path) -> None:
    """The one-live-claim rule is an index, not a Python check somebody can forget.

    Two reps claiming in the same minute is a race the tool cannot win by reading
    first, so the guarantee has to live where the write does.
    """
    conn = _seed(tmp_path)
    _claim(conn, _michigan(conn), "U_KERRY")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO lead_claims
                 (lead_id,slack_user,audience,thread_ts,message_ts,claim_text,
                  claimed_at)
               VALUES (1,'U_NELLY','C1','2.0','2.0','mine','2031-01-01T00:00:00Z')"""
        )


def test_release_hands_it_back_and_keeps_the_history(tmp_path: Path) -> None:
    """A released claim stops holding and stays on file; the lead can be re-claimed."""
    conn = _seed(tmp_path)
    michigan = _michigan(conn)
    _claim(conn, michigan, "U_KERRY")
    assert (
        lead_claims.release(
            conn,
            michigan,
            released_by="U_KERRY",
            note="not mine after all",
            now=FAR_FUTURE,
        )
        == 2
    )
    assert lead_claims.live_claims(conn, michigan.lead_ids) == {}
    assert int(conn.execute("SELECT COUNT(*) FROM lead_claims").fetchone()[0]) == 2, (
        "releasing must not delete the record of who held it"
    )
    _claim(conn, michigan, "U_NELLY")
    assert lead_claims.live_claims(conn, (1,))[1].slack_user == "U_NELLY"


def test_release_honors_the_injected_clock(tmp_path: Path) -> None:
    """Both ends take the caller's clock — the poll-lease defect of 2026-08-26.

    `acquire` accepted an injected clock and `release` read the wall clock, so the
    two disagreed. A claim released "now" by one function and read "then" by another
    is the same defect wearing different clothes.
    """
    conn = _seed(tmp_path)
    michigan = _michigan(conn)
    _claim(conn, michigan, "U_KERRY")
    lead_claims.release(
        conn,
        michigan,
        released_by="U_KERRY",
        now=datetime(2031, 6, 1, tzinfo=timezone.utc),
    )
    stamps = [
        str(row[0])
        for row in conn.execute("SELECT released_at FROM lead_claims").fetchall()
    ]
    assert all(stamp.startswith("2031-06-01") for stamp in stamps)


def test_an_anonymous_claim_is_refused(tmp_path: Path) -> None:
    """A claim nobody is named on can never be reported to a third party."""
    conn = _seed(tmp_path)
    with pytest.raises(ValueError):
        lead_claims.claim(
            conn,
            _michigan(conn),
            slack_user="",
            audience="C1",
            thread_ts="1.0",
            message_ts="1.0",
            claim_text="mine",
        )


def test_a_claim_without_the_words_is_refused(tmp_path: Path) -> None:
    """No receipt, no claim — the wording Grant later quotes has to exist."""
    conn = _seed(tmp_path)
    with pytest.raises(ValueError):
        lead_claims.claim(
            conn,
            _michigan(conn),
            slack_user="U_KERRY",
            audience="C1",
            thread_ts="1.0",
            message_ts="1.0",
            claim_text="   ",
        )
