"""Grant may remember a colleague, but only what they actually said.

A remembered "fact" about a named person outlives every thread and may be repeated
back to them months later, in front of others. So the tests that matter are the ones
about a memory that is plausible, useful, and not quite what the person said.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import db, user_memory

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
KERRY = "U01E908206M"
SAID = "I only cover Texas and Oklahoma, and my son has a lacrosse game Saturday"


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database, never the developer's own."""
    return db.connect(tmp_path / "mem.db")


def test_a_verbatim_quote_is_remembered(conn: sqlite3.Connection) -> None:
    """The ordinary case, end to end."""
    mid = user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="territory",
        fact="Covers Texas and Oklahoma only",
        evidence="I only cover Texas and Oklahoma",
        said=SAID,
        now=NOW,
    )
    assert mid
    got = user_memory.recall(conn, KERRY, now=NOW)
    assert [m.fact for m in got] == ["Covers Texas and Oklahoma only"]
    assert got[0].evidence == "I only cover Texas and Oklahoma"
    conn.close()


def test_a_paraphrase_is_refused(conn: sqlite3.Connection) -> None:
    """The failure this guard exists for, and the likely one.

    "Works the Texas region" means the same thing and is not what she typed. Grant
    would later say "you mentioned" and attach those words to her by name.
    """
    with pytest.raises(ValueError, match="verbatim"):
        user_memory.remember(
            conn,
            slack_user=KERRY,
            kind="territory",
            fact="Covers Texas and Oklahoma only",
            evidence="Works the Texas region",
            said=SAID,
            now=NOW,
        )
    assert user_memory.recall(conn, KERRY, now=NOW) == []
    conn.close()


def test_an_invented_personal_detail_is_refused(conn: sqlite3.Connection) -> None:
    """The highest-consequence case: a fabricated fact about someone's family."""
    with pytest.raises(ValueError, match="verbatim"):
        user_memory.remember(
            conn,
            slack_user=KERRY,
            kind="personal",
            fact="Her daughter plays soccer",
            evidence="my daughter has a soccer game",
            said=SAID,
            now=NOW,
        )
    conn.close()


def test_a_trivially_short_quote_proves_nothing(conn: sqlite3.Connection) -> None:
    """ "ok" appears in every thread; it cannot justify a claim about a person."""
    with pytest.raises(ValueError, match="verbatim"):
        user_memory.remember(
            conn,
            slack_user=KERRY,
            kind="context",
            fact="Agrees with everything",
            evidence="only",
            said=SAID,
            now=NOW,
        )
    conn.close()


def test_a_memory_lapses_after_six_months_without_any_purge_running(
    conn: sqlite3.Connection,
) -> None:
    """Chase set the horizon, and expiry must not depend on a cleanup job firing.

    `recall` filters on `expires_at`, so a lapsed memory is invisible even if `purge`
    has never run once. A policy enforced only by a cron job is a policy that silently
    stops applying the first time the job breaks.
    """
    user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="personal",
        fact="Son plays lacrosse",
        evidence="my son has a lacrosse game",
        said=SAID,
        now=NOW,
    )
    assert user_memory.recall(conn, KERRY, now=NOW + timedelta(days=181))
    assert user_memory.recall(conn, KERRY, now=NOW + timedelta(days=183)) == []
    # And the row is genuinely still there — proving the filter did the work.
    assert conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 1
    removed = user_memory.purge(conn, now=NOW + timedelta(days=183))
    assert removed == 1
    conn.close()


def test_a_corrected_memory_replaces_the_old_one(conn: sqlite3.Connection) -> None:
    """People change territory. Two contradictory beliefs is worse than one stale one."""
    old = user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="territory",
        fact="Covers Texas and Oklahoma only",
        evidence="I only cover Texas and Oklahoma",
        said=SAID,
        now=NOW,
    )
    later = "I only cover California, not Texas or Oklahoma, now"
    new = user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="territory",
        fact="Covers California only",
        evidence="I only cover California, not Texas or Oklahoma, now",
        said=later,
        now=NOW + timedelta(days=30),
    )
    assert old and new
    assert user_memory.supersede(conn, old, new, now=NOW + timedelta(days=30))
    facts = [
        m.fact for m in user_memory.recall(conn, KERRY, now=NOW + timedelta(days=31))
    ]
    assert facts == ["Covers California only"], facts
    conn.close()


def test_remembering_the_same_thing_twice_is_not_new_information(
    conn: sqlite3.Connection,
) -> None:
    """A rep repeating themselves must not produce two identical memories."""
    for _ in range(3):
        user_memory.remember(
            conn,
            slack_user=KERRY,
            kind="territory",
            fact="Covers Texas and Oklahoma only",
            evidence="I only cover Texas and Oklahoma",
            said=SAID,
            now=NOW,
        )
    assert len(user_memory.recall(conn, KERRY, now=NOW)) == 1
    conn.close()


def test_forget_actually_deletes(conn: sqlite3.Connection) -> None:
    """ "I've stopped remembering" has to be true, not a filter."""
    user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="personal",
        fact="Son plays lacrosse",
        evidence="my son has a lacrosse game",
        said=SAID,
        now=NOW,
    )
    assert user_memory.forget(conn, KERRY) == 1
    assert conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 0
    conn.close()


def test_one_persons_memory_never_reaches_another(conn: sqlite3.Connection) -> None:
    """Memory is per colleague. Leaking it sideways is a privacy failure, not a bug."""
    user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="personal",
        fact="Son plays lacrosse",
        evidence="my son has a lacrosse game",
        said=SAID,
        now=NOW,
    )
    assert user_memory.recall(conn, "U_NELLY", now=NOW) == []
    conn.close()


def test_prompt_context_quotes_the_person_and_is_empty_when_nothing_is_known(
    conn: sqlite3.Connection,
) -> None:
    """The rendered context must carry the evidence, not just the conclusion."""
    assert user_memory.as_prompt_context([]) == ""
    user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="personal",
        fact="Son plays lacrosse",
        evidence="my son has a lacrosse game",
        said=SAID,
        now=NOW,
    )
    text = user_memory.as_prompt_context(user_memory.recall(conn, KERRY, now=NOW))
    assert "Son plays lacrosse" in text
    assert "my son has a lacrosse game" in text
    assert "never recite them back as a list" in text
    conn.close()


def test_memory_reaches_the_model_after_the_cache_breakpoint() -> None:
    """A store nothing reads is a filing cabinet. And ORDER decides the API bill.

    `_SYSTEM` is identical for every colleague and carries the cache breakpoint, so
    the per-person block must come AFTER it. Putting the varying text first would
    change the cached prefix on every turn and silently defeat prompt caching for
    everyone at once — the thing Chase noticed as repeated $10 charges.
    """
    from grant_watch.slack import conversation

    plain = conversation._cached_system()
    assert len(plain) == 1
    assert plain[0]["cache_control"] == {"type": "ephemeral"}

    with_memory = conversation._cached_system("- Son plays lacrosse")
    assert len(with_memory) == 2
    assert with_memory[0]["text"] == plain[0]["text"], (
        "the cached prefix changed when memory was added"
    )
    assert with_memory[0]["cache_control"] == {"type": "ephemeral"}
    assert with_memory[1]["text"] == "- Son plays lacrosse"
    assert "cache_control" not in with_memory[1], (
        "the per-person block must not carry a breakpoint"
    )


def test_an_unknown_person_adds_no_block_at_all() -> None:
    """Someone Grant has never met costs nothing and changes no prompt."""
    from grant_watch.slack import conversation

    assert conversation._recall_for("") == ""
    assert len(conversation._cached_system("")) == 1


def test_capture_keeps_the_durable_fact_and_drops_the_invented_one(
    conn: sqlite3.Connection,
) -> None:
    """The model picks what matters; it never gets to author the evidence.

    One real quote, one plausible fabrication. Only the provable one survives, and
    the check is the same `remember` guard rather than a second, weaker copy.
    """
    import json

    payload = json.dumps(
        {
            "facts": [
                {
                    "fact": "Covers Texas and Oklahoma",
                    "kind": "territory",
                    "quote": "I only cover Texas and Oklahoma",
                },
                {
                    "fact": "Prefers spreadsheets over Slack",
                    "kind": "preference",
                    "quote": "just send me a spreadsheet",
                },
            ]
        }
    )
    kept = user_memory.capture(
        conn, slack_user=KERRY, said=SAID, ask_model=lambda _p: payload, now=NOW
    )
    assert kept == 1
    assert [m.fact for m in user_memory.recall(conn, KERRY, now=NOW)] == [
        "Covers Texas and Oklahoma"
    ]
    conn.close()


def test_a_short_message_costs_no_model_call(conn: sqlite3.Connection) -> None:
    """Most messages are "ok, thanks". Paying for a capture pass on those is waste.

    Chase raised the API spend directly, so the gate has to be BEFORE the call, not a
    filter on its result.
    """
    calls: list[str] = []

    def spy(prompt: str) -> str:
        """Record that the model was consulted at all."""
        calls.append(prompt)
        return '{"facts": []}'

    assert (
        user_memory.capture(conn, slack_user=KERRY, said="ok thanks", ask_model=spy)
        == 0
    )
    assert calls == [], "a trivial message triggered a paid model call"
    conn.close()


def test_capture_never_raises_on_junk(conn: sqlite3.Connection) -> None:
    """It runs after the reply is already sent; a failure must stay invisible."""

    def explode(_prompt: str) -> str:
        """A model call that fails outright."""
        raise RuntimeError("model down")

    for bad in ('{"facts": [', "not json", "", None):
        assert (
            user_memory.capture(
                conn, slack_user=KERRY, said=SAID, ask_model=lambda _p, b=bad: b
            )
            == 0
        )
    assert (
        user_memory.capture(conn, slack_user=KERRY, said=SAID, ask_model=explode) == 0
    )
    conn.close()


def test_capture_rejects_a_kind_it_does_not_recognise(conn: sqlite3.Connection) -> None:
    """An invented category must not become a stored one."""
    import json

    payload = json.dumps(
        {
            "facts": [
                {
                    "fact": "Covers Texas",
                    "kind": "salary_expectations",
                    "quote": "I only cover Texas and Oklahoma",
                }
            ]
        }
    )
    assert (
        user_memory.capture(
            conn, slack_user=KERRY, said=SAID, ask_model=lambda _p: payload, now=NOW
        )
        == 0
    )
    conn.close()


@pytest.mark.parametrize(
    ("said", "quote"),
    [
        ("I don't want you to email me the weekly list", "want you to email me"),
        ("not interested in cameras for Fairfax", "interested in cameras for Fairfax"),
        ("I can't cover Oklahoma anymore, only Texas", "cover Oklahoma anymore"),
        ("I would never say we should drop Texas", "we should drop Texas"),
    ],
)
def test_a_quote_that_inverts_the_meaning_is_refused(said: str, quote: str) -> None:
    """Every one of these is a character-for-character substring of what they typed.

    That is what makes them dangerous: the model is not disobeying its instructions,
    a clause really was copied. The guard was measuring the wrong thing. These get
    repeated back to a named colleague weeks later as "you mentioned".
    """
    assert not user_memory.evidence_supports(quote, said), (
        f"stored {quote!r} as a quote from {said!r}"
    )


def test_ordinary_quoting_still_works() -> None:
    """The negation guard must not make the feature useless."""
    said = "I only cover Texas and Oklahoma, and my son has a lacrosse game"
    assert user_memory.evidence_supports("I only cover Texas and Oklahoma", said)
    assert user_memory.evidence_supports("my son has a lacrosse game", said)


def test_a_fact_the_quote_says_nothing_about_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """`evidence` was checked against the message and `fact` against nothing.

    But `fact` is what reaches the model and what Grant acts on; the quote sits
    beside it as decoration. A long message admitted "Is leaving the company in
    September" on the quote "about a lead" — both individually valid, jointly a
    fabrication about a colleague's career.
    """
    said = "Can you look at the Fairfax one, I had a question about a lead there"
    with pytest.raises(ValueError, match="does not support"):
        user_memory.remember(
            conn,
            slack_user=KERRY,
            kind="personal",
            fact="Is leaving the company in September",
            evidence="about a lead",
            said=said,
            now=NOW,
        )
    assert user_memory.recall(conn, KERRY, now=NOW) == []
    conn.close()


def test_an_expired_fact_can_be_learned_again(conn: sqlite3.Connection) -> None:
    """UNIQUE outlives expiry, so a lapsed memory was permanent amnesia.

    `recall` hid it and `remember` swallowed the IntegrityError, so a person could
    repeat the exact thing they had said six months earlier and Grant would neither
    know it nor be able to record it.
    """
    later = NOW + timedelta(days=200)
    user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="territory",
        fact="Covers Texas and Oklahoma only",
        evidence="I only cover Texas and Oklahoma",
        said=SAID,
        now=NOW,
    )
    assert user_memory.recall(conn, KERRY, now=later) == [], "it should have lapsed"

    user_memory.remember(
        conn,
        slack_user=KERRY,
        kind="territory",
        fact="Covers Texas and Oklahoma only",
        evidence="I only cover Texas and Oklahoma",
        said=SAID,
        now=later,
    )
    live = user_memory.recall(conn, KERRY, now=later)
    assert [m.fact for m in live] == ["Covers Texas and Oklahoma only"], (
        "a lapsed memory could never be re-learned"
    )
    assert conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 1
    conn.close()
