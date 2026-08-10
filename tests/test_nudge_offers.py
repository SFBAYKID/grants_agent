"""What Grant remembers having OFFERED, and how that reaches the next turn.

Split from `test_nudges.py` at the 1,000-line cap. The boundary is real: these are
about the conversation AFTER a follow-up lands, while the rest of the nudge tests are
about whether one should be sent at all.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


from grant_watch import capability_asks, db
from grant_watch.slack import nudges

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)
CHANNEL = "C0TEST"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database, never the developer's own."""
    return db.connect(tmp_path / "offer.db")


def _seed_capability_subject(conn: sqlite3.Connection) -> None:
    """One armed capability ask — the simplest subject a nudge can be built from."""
    capability_asks.record(
        conn,
        slack_user="U01E908206M",
        audience=CHANNEL,
        thread_ts="800.9",
        message_ts="800.9",
        ask_text="can you add the lead lists assigned to each state to campaign?",
        capability="campaign_load",
        asked_at="2026-07-23T15:26:29+00:00",
        recorded_by="test",
    )
    capability_asks.mark_available(
        conn, "campaign_load", shipped_at=(NOW - timedelta(days=3)).isoformat()
    )


def test_the_thread_remembers_what_grant_offered(tmp_path: Path) -> None:
    """Grant's first proactive follow-up, and the reply three minutes later.

    It reached Kerry at 10:00 quoting her own July words — "Email those to
    kerry@monarchconnected.com… I can now — want me to send it?" She said "Yes". Grant
    classified that as prospect outreach and answered "Tell me the exact Lead number
    you want to use." She had asked for her own spreadsheets.

    Prose cannot fix this: her quoted sentence CONTAINS an email address, so the thread
    genuinely looks like a request to email somebody, and a bare "Yes" has no words of
    its own to correct it. The offer has to be read from the ledger instead.
    """
    conn = _conn(tmp_path)
    _seed_capability_subject(conn)
    sent: list[dict[str, object]] = []

    class _Client:
        """A Slack client that records the delivered nudge."""

        def chat_postMessage(self, **kw: object) -> dict[str, object]:
            """Accept and record."""
            sent.append(kw)
            return {"ts": "9.9"}

    assert "nudged" in nudges.run(_Client(), conn, now=NOW)
    row = conn.execute(
        "SELECT audience,anchor_ts,state FROM followup_nudges"
    ).fetchone()
    assert row["state"] == "delivered"

    offered = nudges.pending_capability_offer(
        conn, str(row["audience"]), str(row["anchor_ts"])
    )
    assert offered == "campaign_load", (
        "the thread cannot say what Grant offered, so a bare 'Yes' is unanswerable"
    )

    # A different thread must not inherit the offer.
    assert nudges.pending_capability_offer(conn, str(row["audience"]), "999.9") == ""
    # Nor a different channel.
    assert nudges.pending_capability_offer(conn, "C0OTHER", str(row["anchor_ts"])) == ""
    conn.close()


def test_an_unresolvable_connection_costs_an_answer_not_the_turn(
    tmp_path: Path,
) -> None:
    """This lookup runs inside a live reply; it must degrade, never raise."""
    from types import SimpleNamespace

    assert nudges.pending_capability_offer(SimpleNamespace(), "C1", "1.1") == ""


def test_the_offer_is_given_to_the_model_and_never_auto_sent(tmp_path: Path) -> None:
    """The hint reaches the model; nothing is emailed behind its back.

    My first attempt at this intercepted the misclassification and called
    `email_results` itself — with no search spec, which renders empty, which would
    have mailed Kerry "I couldn't find anything matching that." A confident false
    negative in her inbox is worse than the wrong question in Slack.
    """
    from grant_watch.slack import grant as grant_mod

    src = Path(grant_mod.__file__).read_text()
    assert "_deliver_offered_capability" not in src, (
        "the blind auto-send is back; it emails an empty search"
    )
    assert "context = _with_pending_offer(context, channel, thread_ts)" in src, (
        "the model is no longer told what Grant offered"
    )


def test_the_offer_hint_names_the_capability_and_says_it_is_not_outreach() -> None:
    """The hint has to correct the specific misread, not just add noise."""
    from grant_watch.slack import grant as grant_mod

    hint = grant_mod._with_pending_offer(["earlier"], "", "")
    assert hint == ["earlier"], "no thread means no hint"

    src = Path(grant_mod.__file__).read_text()
    block = src[src.index("def _with_pending_offer") : src.index("def _remember_from")]
    assert "not a request to" in block and "prospect" in block, (
        "the hint does not rule out the reading that caused the failure"
    )
    assert "SYSTEM FACT" in block
