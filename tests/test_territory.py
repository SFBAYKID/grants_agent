"""Territory-tagging tests: the right rep, nobody at all, and never a guessed id.

The failure that matters here is not a crash — it is tagging the WRONG rep, which
silently hands one person's revenue to another. So the happy paths are cheap and most
of this file is about what must NOT render a mention.
"""

from __future__ import annotations

import pytest

from grant_watch import territory


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("PA", "U08C1NBH875"),  # Brett D'Ambrosio
        ("CA", "U01DFJWQQJ3"),  # Anthony Dambrosio
        ("WA", "U01E908206M"),  # Kerry Hilligus
        ("TX", "U01E908206M"),
        ("OR", "U01E908206M"),
    ],
)
def test_configured_states_map_to_their_rep(state: str, expected: str) -> None:
    """Provide test-local behavior for configured states map to their rep."""
    assert territory.owner_for_state(state) == expected


def test_state_code_is_case_and_whitespace_insensitive() -> None:
    """Sources emit ' pa ' as readily as 'PA'; the rep must not depend on formatting."""
    assert territory.owner_for_state(" pa ") == "U08C1NBH875"


@pytest.mark.parametrize("state", ["NY", "MI", "", None, "PENN", "P", "12", "CA;DROP"])
def test_unmapped_or_malformed_state_tags_nobody(state: object) -> None:
    """An unowned or unparseable state yields no owner — never a fallback rep."""
    assert territory.owner_for_state(state) is None
    assert territory.mention_line(state, "usaspending:16.071") == ""


def test_mention_line_names_the_rep_the_state_and_asks_for_a_reply() -> None:
    """The line must notify a human and give them something to answer."""
    line = territory.mention_line("PA", "usaspending:16.071")
    assert line.startswith("\n\n<@U08C1NBH875>")
    assert "Pennsylvania is your territory" in line
    assert line.rstrip().endswith("?")


def test_mention_line_spells_out_texas() -> None:
    """Regression: drip only knew 5 states, so a TX card used to read 'in TX'."""
    assert "Texas is your territory" in territory.mention_line("TX", "webs")


def test_env_override_replaces_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """New states/reps ship by config, not by a deploy (CLAUDE.md)."""
    monkeypatch.setenv("GRANT_TERRITORY_OWNERS", "NY=U0NEWREP99,pa=U0OTHERREP1")
    assert territory.owner_for_state("NY") == "U0NEWREP99"
    assert territory.owner_for_state("PA") == "U0OTHERREP1"
    # Replacement, not merge: CA is no longer owned, so it goes untagged.
    assert territory.owner_for_state("CA") is None


def test_blank_override_falls_back_to_the_verified_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provide test-local behavior for blank override falls back to defaults."""
    monkeypatch.setenv("GRANT_TERRITORY_OWNERS", "   ")
    assert territory.owner_for_state("PA") == "U08C1NBH875"


@pytest.mark.parametrize(
    "raw",
    [
        "PA=notauserid",  # not a Slack id shape
        "PA=<@U08C1NBH875>",  # already-rendered mention, would double-wrap
        "PA=U08C1NBH875 extra",  # trailing junk
        "PA U08C1NBH875",  # missing '='
        "PENNSYLVANIA=U08C1NBH875",  # not a USPS code
        "PA=",  # empty id
    ],
)
def test_malformed_override_entry_is_dropped_not_rendered(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad env entry must fail safe (no tag), never emit broken or injectable text."""
    monkeypatch.setenv("GRANT_TERRITORY_OWNERS", raw)
    assert territory.owner_for_state("PA") is None
    assert territory.mention_line("PA", "usaspending:16.071") == ""


def test_malformed_override_does_not_crash_the_tick(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo in .env must not take the drip cron down; it reports and continues."""
    monkeypatch.setenv("GRANT_TERRITORY_OWNERS", "PA=oops,CA=U01DFJWQQJ3")
    assert territory.owner_for_state("CA") == "U01DFJWQQJ3"
    assert "territory" in capsys.readouterr().err


def test_every_default_id_is_a_well_formed_slack_id() -> None:
    """Guards the map itself: a hand-edited typo here would tag nobody in production."""
    for state, user in territory.DEFAULT_TERRITORY_OWNERS.items():
        assert territory._SLACK_ID_RE.match(user), f"{state} -> {user!r}"


def test_mention_line_contains_no_injectable_markup() -> None:
    """The rendered line is built only from validated ids and a fixed state table."""
    line = territory.mention_line("CA", "ca-grants-award:2024-2025")
    assert line.count("<") == 1 and line.count(">") == 1
    assert "http" not in line and "`" not in line


def test_routing_line_tags_a_mapped_state() -> None:
    """A mapped, verified-source state gets its rep mention."""
    line = territory.routing_line("PA", "usaspending:16.071")
    assert "<@U08C1NBH875>" in line and "Pennsylvania is your territory" in line


def test_routing_line_labels_an_unmapped_state_as_unassigned() -> None:
    """Nationwide: an unmapped state is an honest opportunity, labelled explicitly and
    never tagged to a guessed rep (Chase 2026-07-22)."""
    line = territory.routing_line("AZ", "usaspending:16.071")
    assert "<@" not in line
    assert "Arizona is unassigned territory" in line
    assert "no rep mapped yet" in line


def test_routing_line_stays_silent_for_an_inferred_state() -> None:
    """A source that only INFERRED the state (RFP aggregator) asserts no territory at
    all — not even 'unassigned' — because the state itself is untrusted."""
    assert territory.routing_line("OR", "rfp") == ""
    assert territory.routing_line("AZ", None) == ""


def test_routing_line_has_no_injectable_markup() -> None:
    """Built only from the fixed state table and validated ids."""
    for line in (territory.routing_line("CA", "ca-grants-award:2024-2025"),
                 territory.routing_line("AZ", "usaspending:16.071")):
        assert "http" not in line and "`" not in line
        assert line.count("<") == line.count(">")  # balanced or zero
