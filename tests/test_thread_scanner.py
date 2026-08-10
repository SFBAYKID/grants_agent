"""The scanner must find real asks and refuse invented ones.

The whole value of a discovered ask is that Grant can later say "you asked for this"
to a named colleague. That sentence is only safe if the quote is provably theirs, so
the tests that matter here are the ones about quotes that are ALMOST right.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grant_watch import capability_asks, db, thread_scanner


def _thread(*messages: dict[str, object]) -> thread_scanner.ThreadTranscript:
    """A transcript from raw Slack-shaped dicts."""
    return thread_scanner.ThreadTranscript(
        channel="C1", thread_ts="1720000000.0001", messages=tuple(messages)
    )


REP = {"user": "U_KERRY", "ts": "1720000000.0001", "text": "Email those to kerry@x.com"}
BOT = {"bot_id": "B1", "ts": "1720000001.0001", "text": "I can't send email."}


def test_a_verbatim_quote_is_accepted() -> None:
    """The ordinary case: the model copied the rep's words exactly."""
    transcript = _thread(REP, BOT)
    assert thread_scanner.quote_is_real("Email those to kerry@x.com", transcript)


def test_a_tidied_quote_is_rejected() -> None:
    """A better-written version of what someone said is not what they said.

    This is the failure the guard exists for, and it is the LIKELY one: asked to
    quote, a model produces a cleaner sentence carrying the same meaning. Meaning is
    not enough — Grant attaches these words to a person by name, weeks later.
    """
    transcript = _thread(REP, BOT)
    assert not thread_scanner.quote_is_real("Please email me the list", transcript)


def test_a_quote_taken_from_grants_own_reply_is_rejected() -> None:
    """Grant must never cite itself back to a rep as something THEY asked for."""
    transcript = _thread(REP, BOT)
    assert not thread_scanner.quote_is_real("I can't send email.", transcript)


def test_line_wrapping_does_not_break_a_real_quote() -> None:
    """Slack wraps; a genuine quote must survive it or every real ask is discarded."""
    transcript = _thread({**REP, "text": "Email those\n  to   kerry@x.com"}, BOT)
    assert thread_scanner.quote_is_real("Email those to kerry@x.com", transcript)


def test_extract_discards_the_fabricated_ask_and_keeps_the_real_one() -> None:
    """One good ask, one invented — only the provable one survives, and it is counted."""
    transcript = _thread(REP, BOT)
    payload = json.dumps(
        {
            "asks": [
                {"quote": "Email those to kerry@x.com", "capability": "email_results"},
                {"quote": "Also export it to Sheets", "capability": "export_sheets"},
            ]
        }
    )
    asks, discarded = thread_scanner.extract_asks(transcript, lambda _p: payload)
    assert [a["capability"] for a in asks] == ["email_results"]
    assert discarded == 1, "an unverifiable ask must be counted, not silently dropped"


def test_a_capability_nobody_hard_coded_is_accepted() -> None:
    """The point of the rewrite: tomorrow's ask records without a code change.

    `record` used to reject anything outside a four-item enum, so a new kind of
    request could not be stored until someone edited the source — the trap Chase
    named.
    """
    transcript = _thread({**REP, "text": "can you remove her from the campaign"}, BOT)
    payload = json.dumps(
        {
            "asks": [
                {
                    "quote": "can you remove her from the campaign",
                    "capability": "remove_from_campaign",
                }
            ]
        }
    )
    asks, discarded = thread_scanner.extract_asks(transcript, lambda _p: payload)
    assert [a["capability"] for a in asks] == ["remove_from_campaign"]
    assert discarded == 0


def test_a_capability_slug_carrying_sql_is_discarded() -> None:
    """The slug is interpolated into messages and stored; it stays a slug."""
    transcript = _thread(REP, BOT)
    payload = json.dumps(
        {
            "asks": [
                {"quote": "Email those to kerry@x.com", "capability": "DROP TABLE x"}
            ]
        }
    )
    asks, discarded = thread_scanner.extract_asks(transcript, lambda _p: payload)
    assert asks == []
    assert discarded == 1


def test_model_junk_never_raises() -> None:
    """A scan is a background job; malformed output degrades to zero findings."""
    transcript = _thread(REP, BOT)
    for bad in ["", "not json at all", "{broken", None]:
        assert thread_scanner.extract_asks(transcript, lambda _p, b=bad: b) == ([], 0)

    def explode(_prompt: str) -> str:
        """A model call that fails outright."""
        raise RuntimeError("model down")

    assert thread_scanner.extract_asks(transcript, explode) == ([], 0)


def test_last_speaker_is_human_detects_an_unanswered_thread() -> None:
    """'Where you did not respond' — the case Chase described first."""
    assert _thread(BOT, REP).last_speaker_is_human()
    assert not _thread(REP, BOT).last_speaker_is_human()


class _FakeSlack:
    """Minimal Slack stand-in returning one thread."""

    def __init__(self, messages: list[dict[str, object]]) -> None:
        """Hold the one thread this fake will return."""
        self._messages = messages

    def conversations_history(self, **_kw: object) -> dict[str, object]:
        """One parent message, so the scan finds exactly one thread."""
        return {"messages": [{"ts": "1720000000.0001"}]}

    def conversations_replies(self, **_kw: object) -> dict[str, object]:
        """The thread body under test."""
        return {"messages": self._messages}


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A real migrated database, never the developer's own."""
    return db.connect(tmp_path / "scan.db")


def test_a_scan_records_a_real_ask_and_rescanning_does_not_duplicate_it(
    conn: sqlite3.Connection,
) -> None:
    """Idempotence is what makes this a standing job instead of a one-shot seed.

    The system it replaces fired once over a hand-written file. This one runs every
    week over the same channel and must re-find the same asks without ever producing
    a second follow-up for one of them.
    """
    payload = json.dumps(
        {
            "asks": [
                {"quote": "Email those to kerry@x.com", "capability": "email_results"}
            ]
        }
    )
    client = _FakeSlack([REP, BOT])
    first = thread_scanner.scan_channel(
        client, conn, "C1", lambda _p: payload, dry_run=False
    )
    assert "1 newly recorded" in first, first
    second = thread_scanner.scan_channel(
        client, conn, "C1", lambda _p: payload, dry_run=False
    )
    assert "0 newly recorded" in second, "a re-scan created a duplicate follow-up"

    rows = list(conn.execute("SELECT * FROM capability_asks"))
    assert len(rows) == 1
    assert rows[0]["ask_text"] == "Email those to kerry@x.com"
    assert rows[0]["slack_user"] == "U_KERRY"
    assert rows[0]["recorded_by"] == "thread-scan"
    # Discovery must not arm a message. A human decides when a feature has shipped.
    assert rows[0]["available_since"] is None
    conn.close()


def test_a_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    """Default-safe, like every other proactive path here."""
    payload = json.dumps(
        {
            "asks": [
                {"quote": "Email those to kerry@x.com", "capability": "email_results"}
            ]
        }
    )
    out = thread_scanner.scan_channel(
        _FakeSlack([REP, BOT]), conn, "C1", lambda _p: payload
    )
    assert out.startswith("[dry-run]")
    assert conn.execute("SELECT COUNT(*) FROM capability_asks").fetchone()[0] == 0
    conn.close()


def test_a_discovered_ask_flows_into_a_real_follow_up(conn: sqlite3.Connection) -> None:
    """End to end: discovered -> marked available -> eligible to be reopened.

    Without this the scanner is a filing cabinet. The chain being proven is the one
    Chase described: notice the ask, ship the thing, come back to the person.
    """
    payload = json.dumps(
        {
            "asks": [
                {"quote": "Email those to kerry@x.com", "capability": "email_results"}
            ]
        }
    )
    thread_scanner.scan_channel(
        _FakeSlack([REP, BOT]), conn, "C1", lambda _p: payload, dry_run=False
    )
    assert [a.capability for a in capability_asks.open_asks(conn)] == ["email_results"]
    assert capability_asks.mark_available(conn, "email_results") == 1
    row = conn.execute("SELECT available_since FROM capability_asks").fetchone()
    assert row["available_since"], "shipping the feature did not arm the follow-up"
    conn.close()
