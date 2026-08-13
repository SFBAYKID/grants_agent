"""Automatic memory corrections are scoped, explicit, and transactional."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import db, user_memory

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
KERRY = "U01E908206M"
OTHER = "U_OTHER"
OLD_SAID = (
    "For territory planning, I only cover Texas and Oklahoma, so keep my searches "
    "inside those two states."
)
CORRECTION = (
    "I only cover California, not Texas or Oklahoma, now. Please use that territory "
    "for every future grant search you prepare for me."
)


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Return one fully migrated, test-owned memory database."""
    return db.connect(tmp_path / "memory-corrections.db")


def _remember_old(
    conn: sqlite3.Connection,
    *,
    slack_user: str = KERRY,
    kind: str = "territory",
    now: datetime = NOW,
) -> int:
    """Store one reusable baseline memory and return its concrete row ID."""
    memory_id = user_memory.remember(
        conn,
        slack_user=slack_user,
        kind=kind,
        fact="Covers Texas and Oklahoma only",
        evidence="I only cover Texas and Oklahoma",
        said=OLD_SAID,
        now=now,
    )
    assert memory_id is not None
    return memory_id


def _replacement(memory_id: int) -> str:
    """Return one explicit model-authored correction envelope."""
    return json.dumps(
        {
            "facts": [
                {
                    "action": "replace",
                    "replaces_memory_id": memory_id,
                    "fact": "Covers California only",
                    "kind": "territory",
                    "quote": "I only cover California, not Texas or Oklahoma, now",
                }
            ]
        }
    )


def test_explicit_correction_atomically_replaces_one_offered_memory(
    conn: sqlite3.Connection,
) -> None:
    """The automatic path, not a manual helper call, retires the prior fact."""
    old_id = _remember_old(conn)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        """Capture the bounded candidate set and choose its exact old ID."""
        prompts.append(prompt)
        return _replacement(old_id)

    kept = user_memory.capture(
        conn,
        slack_user=KERRY,
        said=CORRECTION,
        ask_model=answer,
        now=NOW + timedelta(days=1),
    )

    active = user_memory.recall(conn, KERRY, now=NOW + timedelta(days=1))
    assert kept == 1
    assert [memory.fact for memory in active] == ["Covers California only"]
    assert f'"memory_id":{old_id}' in prompts[0]
    row = conn.execute(
        "SELECT superseded_by FROM user_memory WHERE id=?", (old_id,)
    ).fetchone()
    assert row[0] == active[0].memory_id


def test_additive_territory_expansion_keeps_both_facts(
    conn: sqlite3.Connection,
) -> None:
    """A same-kind addition is not guessed to contradict an existing territory."""
    _remember_old(conn)
    said = (
        "I picked up Louisiana last month, and I want it included in the regional "
        "grant searches you send from now on."
    )
    payload = json.dumps(
        {
            "facts": [
                {
                    "action": "add",
                    "replaces_memory_id": None,
                    "fact": "Also covers Louisiana",
                    "kind": "territory",
                    "quote": "I picked up Louisiana last month",
                }
            ]
        }
    )

    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=said,
            ask_model=lambda _prompt: payload,
            now=NOW + timedelta(days=1),
        )
        == 1
    )
    assert {memory.fact for memory in user_memory.recall(conn, KERRY, now=NOW)} == {
        "Covers Texas and Oklahoma only",
        "Also covers Louisiana",
    }


@pytest.mark.parametrize(
    "addition",
    [
        "I picked up Louisiana last month",
        "Actually, I picked up Louisiana last month",
    ],
)
def test_adversarial_replace_label_cannot_retire_an_additive_statement(
    conn: sqlite3.Connection, addition: str
) -> None:
    """Model relationship output alone cannot turn expansion into correction."""
    old_id = _remember_old(conn)
    said = (
        f"{addition}, and I want it included in the regional "
        "grant searches you send from now on."
    )
    payload = json.dumps(
        {
            "facts": [
                {
                    "action": "replace",
                    "replaces_memory_id": old_id,
                    "fact": "Also covers Louisiana",
                    "kind": "territory",
                    "quote": addition,
                }
            ]
        }
    )

    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=said,
            ask_model=lambda _prompt: payload,
            now=NOW + timedelta(days=1),
        )
        == 0
    )
    assert [memory.fact for memory in user_memory.recall(conn, KERRY, now=NOW)] == [
        "Covers Texas and Oklahoma only"
    ]
    assert conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 1


@pytest.mark.parametrize(
    "quote",
    [
        "I no longer take the bus to work, but I picked up Louisiana last month",
        "Don't replace Texas and Oklahoma; I picked up Louisiana last month",
        "Don't only cover California now, keep Texas",
        "Never only cover California now, retain Texas",
        "I'm only kidding, don't cover California now, keep Texas",
        "I only cover California jokingly, not Texas or Oklahoma, now",
        (
            "I only cover California and handle extensive regional grant review "
            "planning, not Texas or Oklahoma, now"
        ),
        "I only cover California if my manager approves, not Texas or Oklahoma, now",
    ],
)
def test_unrelated_or_negated_correction_words_cannot_retire_memory(
    conn: sqlite3.Connection, quote: str
) -> None:
    """Correction vocabulary must relate to the asserted complete-state fact."""
    old_id = _remember_old(conn)
    said = f"{quote}, and remember this for all future grant searches you prepare."
    payload = json.dumps(
        {
            "facts": [
                {
                    "action": "replace",
                    "replaces_memory_id": old_id,
                    "fact": (
                        "Covers California and Colorado"
                        if "extensive regional" in quote
                        else "Covers California only"
                        if "California" in quote
                        else "Also covers Louisiana"
                    ),
                    "kind": "territory",
                    "quote": quote,
                }
            ]
        }
    )

    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=said,
            ask_model=lambda _prompt: payload,
            now=NOW + timedelta(days=1),
        )
        == 0
    )
    assert [memory.fact for memory in user_memory.recall(conn, KERRY, now=NOW)] == [
        "Covers Texas and Oklahoma only"
    ]


def test_unrelated_personal_memories_coexist(conn: sqlite3.Connection) -> None:
    """Two personal details are additions merely because they share one kind."""
    said = (
        "My son plays lacrosse on Saturdays, and I volunteer at the animal shelter "
        "every Wednesday evening after work."
    )
    payload = json.dumps(
        {
            "facts": [
                {
                    "action": "add",
                    "replaces_memory_id": None,
                    "fact": "Son plays lacrosse on Saturdays",
                    "kind": "personal",
                    "quote": "My son plays lacrosse on Saturdays",
                },
                {
                    "action": "add",
                    "replaces_memory_id": None,
                    "fact": "Volunteers at the animal shelter every Wednesday",
                    "kind": "personal",
                    "quote": "I volunteer at the animal shelter every Wednesday",
                },
            ]
        }
    )

    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=said,
            ask_model=lambda _prompt: payload,
            now=NOW,
        )
        == 2
    )
    assert len(user_memory.recall(conn, KERRY, now=NOW)) == 2


def test_unoffered_wrong_user_expired_and_unknown_targets_are_discarded(
    conn: sqlite3.Connection,
) -> None:
    """A model-supplied ID has no authority outside the scoped candidate list."""
    other_id = _remember_old(conn, slack_user=OTHER)
    expired_id = _remember_old(conn, now=NOW - timedelta(days=200))
    for target in (other_id, expired_id, 987_654_321):
        assert (
            user_memory.capture(
                conn,
                slack_user=KERRY,
                said=CORRECTION,
                ask_model=lambda _prompt, target=target: _replacement(target),
                now=NOW,
            )
            == 0
        )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM user_memory WHERE fact='Covers California only'"
        ).fetchone()[0]
        == 0
    )


def test_wrong_kind_replacement_is_discarded_before_insert(
    conn: sqlite3.Connection,
) -> None:
    """An offered ID still cannot cross the memory-kind boundary."""
    old_id = _remember_old(conn, kind="preference")
    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=CORRECTION,
            ask_model=lambda _prompt: _replacement(old_id),
            now=NOW,
        )
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 1


def test_target_that_turns_stale_during_model_call_discards_new_row(
    conn: sqlite3.Connection,
) -> None:
    """The database relationship is revalidated after the model's snapshot."""
    old_id = _remember_old(conn)

    def stale_then_answer(_prompt: str) -> str:
        """Simulate another turn correcting the row while this model is running."""
        said = (
            "I only cover Nevada, not Texas or Oklahoma, now. Remove the old "
            "territory from every future search and use Nevada instead."
        )
        newer = user_memory.remember(
            conn,
            slack_user=KERRY,
            kind="territory",
            fact="Covers Nevada only",
            evidence="I only cover Nevada, not Texas or Oklahoma, now",
            said=said,
            now=NOW + timedelta(hours=1),
        )
        assert newer is not None
        assert user_memory.supersede(conn, old_id, newer, now=NOW + timedelta(hours=1))
        return _replacement(old_id)

    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=CORRECTION,
            ask_model=stale_then_answer,
            now=NOW + timedelta(hours=2),
        )
        == 0
    )
    assert [
        memory.fact
        for memory in user_memory.recall(conn, KERRY, now=NOW + timedelta(hours=2))
    ] == ["Covers Nevada only"]
    assert conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 2


def test_database_failure_between_insert_and_supersession_rolls_back_both(
    conn: sqlite3.Connection,
) -> None:
    """A correction can never leave the new and old contradictory rows active."""
    old_id = _remember_old(conn)
    conn.execute(
        f"""CREATE TRIGGER fail_memory_supersession
              BEFORE UPDATE OF superseded_by ON user_memory
              WHEN OLD.id={old_id}
              BEGIN
                SELECT RAISE(ABORT, 'simulated supersession failure');
              END"""
    )
    conn.commit()

    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=CORRECTION,
            ask_model=lambda _prompt: _replacement(old_id),
            now=NOW + timedelta(days=1),
        )
        == 0
    )
    assert [memory.fact for memory in user_memory.recall(conn, KERRY, now=NOW)] == [
        "Covers Texas and Oklahoma only"
    ]
    assert conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 1


def test_repeated_correction_is_idempotent_and_cannot_cycle(
    conn: sqlite3.Connection,
) -> None:
    """A replay cannot re-target an ID removed from the offered active set."""
    old_id = _remember_old(conn)

    def call(_prompt: str) -> str:
        """Replay the same stale model result twice."""
        return _replacement(old_id)

    first = user_memory.capture(
        conn,
        slack_user=KERRY,
        said=CORRECTION,
        ask_model=call,
        now=NOW + timedelta(days=1),
    )
    second = user_memory.capture(
        conn,
        slack_user=KERRY,
        said=CORRECTION,
        ask_model=call,
        now=NOW + timedelta(days=1, seconds=1),
    )

    assert (first, second) == (1, 0)
    rows = conn.execute(
        "SELECT id,superseded_by FROM user_memory ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == rows[1][0]
    assert rows[1][1] is None


def test_correction_and_independent_addition_both_survive(
    conn: sqlite3.Connection,
) -> None:
    """One valid correction cannot roll back an unrelated fact in the same result."""
    old_id = _remember_old(conn)
    said = (
        "I only cover California, not Texas or Oklahoma, now. My daughter plays soccer every "
        "Saturday morning before I start reviewing grant leads."
    )
    payload = json.dumps(
        {
            "facts": [
                {
                    "action": "replace",
                    "replaces_memory_id": old_id,
                    "fact": "Covers California only",
                    "kind": "territory",
                    "quote": "I only cover California, not Texas or Oklahoma, now",
                },
                {
                    "action": "add",
                    "replaces_memory_id": None,
                    "fact": "Daughter plays soccer every Saturday morning",
                    "kind": "personal",
                    "quote": "my daughter plays soccer every Saturday morning",
                },
            ]
        }
    )

    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=said,
            ask_model=lambda _prompt: payload,
            now=NOW + timedelta(days=1),
        )
        == 2
    )
    assert {memory.fact for memory in user_memory.recall(conn, KERRY, now=NOW)} == {
        "Covers California only",
        "Daughter plays soccer every Saturday morning",
    }


def test_candidate_prompt_is_bounded_and_never_leaks_another_user(
    conn: sqlite3.Connection,
) -> None:
    """Only this user's newest reviewed IDs can be selected for replacement."""
    other_said = (
        "My private preference is paper reports, and I want that remembered for all "
        "future quarterly planning conversations."
    )
    user_memory.remember(
        conn,
        slack_user=OTHER,
        kind="preference",
        fact="Prefers paper reports",
        evidence="My private preference is paper reports",
        said=other_said,
        now=NOW,
    )
    ids: list[int] = []
    for index in range(user_memory.MAX_CAPTURE_CANDIDATES + 1):
        said = (
            f"I have standing context number {index} for quarterly planning, and "
            "please remember that context for future grant work."
        )
        memory_id = user_memory.remember(
            conn,
            slack_user=KERRY,
            kind="context",
            fact=f"Standing context number {index}",
            evidence=f"standing context number {index}",
            said=said,
            now=NOW + timedelta(seconds=index),
        )
        assert memory_id is not None
        ids.append(memory_id)
    captured_prompt = ""

    def answer(prompt: str) -> str:
        """Save the prompt while proposing the oldest, deliberately unoffered ID."""
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "facts": [
                    {
                        "action": "replace",
                        "replaces_memory_id": ids[0],
                        "fact": "Standing context number 99",
                        "kind": "context",
                        "quote": "standing context number 99",
                    }
                ]
            }
        )

    said = (
        "I now use standing context number 99 for quarterly planning, so please "
        "replace the old context in every future grant discussion."
    )
    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=said,
            ask_model=answer,
            now=NOW + timedelta(hours=1),
        )
        == 0
    )
    assert "Prefers paper reports" not in captured_prompt
    assert f'"memory_id":{ids[0]},' not in captured_prompt
    assert captured_prompt.count('"memory_id":') == user_memory.MAX_CAPTURE_CANDIDATES


def test_public_supersede_rejects_cross_scope_expiry_staleness_and_cycles(
    conn: sqlite3.Connection,
) -> None:
    """The public primitive is safe even when called outside automatic capture."""
    old_id = _remember_old(conn)
    wrong_user = _remember_old(conn, slack_user=OTHER, now=NOW + timedelta(seconds=1))
    preference = _remember_old(conn, kind="preference", now=NOW + timedelta(seconds=2))
    new_said = (
        "I only cover California, not Texas or Oklahoma, now. This is the complete "
        "territory to use for every future search."
    )
    new_id = user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="territory",
        fact="Covers California only",
        evidence="I only cover California, not Texas or Oklahoma, now",
        said=new_said,
        now=NOW + timedelta(seconds=3),
    )
    assert new_id is not None

    assert not user_memory.supersede(conn, old_id, wrong_user, now=NOW)
    assert not user_memory.supersede(conn, old_id, preference, now=NOW)
    assert not user_memory.supersede(conn, old_id, 999_999, now=NOW)
    future = NOW + timedelta(seconds=3)
    assert not user_memory.supersede(conn, old_id, new_id, now=NOW)
    assert user_memory.supersede(conn, old_id, new_id, now=future)
    assert not user_memory.supersede(conn, old_id, new_id, now=future)
    assert not user_memory.supersede(conn, new_id, old_id, now=future)
    with pytest.raises(ValueError, match="itself"):
        user_memory.supersede(conn, new_id, new_id, now=future)

    expired_old = _remember_old(
        conn,
        slack_user="U_EXPIRED",
        now=NOW - timedelta(days=200),
    )
    expired_new = user_memory.remember(
        conn,
        slack_user="U_EXPIRED",
        kind="territory",
        fact="Covers California only",
        evidence="I only cover California, not Texas or Oklahoma, now",
        said=new_said,
        now=NOW,
    )
    assert expired_new is not None
    assert not user_memory.supersede(conn, expired_old, expired_new, now=NOW)


def test_valid_non_object_model_json_returns_zero(conn: sqlite3.Connection) -> None:
    """A syntactically valid JSON array is not assumed to have a facts mapping."""
    assert (
        user_memory.capture(
            conn,
            slack_user=KERRY,
            said=OLD_SAID,
            ask_model=lambda _prompt: "[]",
            now=NOW,
        )
        == 0
    )
