"""The evidence behind "X has not come back to me" — and every way it was wrong.

Split from `test_nudge_followups.py` at the 1,000-line cap. The boundary is real: that
file tests WHAT the follow-up system does, this one tests whether Grant is entitled to
SAY it. Every case here was reproduced as a live false accusation by an adversarial
review before it was fixed, which is why each carries the shape of the real reply that
broke it rather than a synthetic one.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from grant_watch.slack import nudge_silence, nudges

from nudge_helpers import (
    CHANNEL,
    JOCELYN,
    NOW,
    _conn,
    _delivered_offer,
    _Slack,
)


# ------------------------------------------- the four ways it accused people wrongly


def test_an_ordinary_human_reply_is_never_read_as_software(tmp_path: Path) -> None:
    """A reply carrying a Slack `subtype` is still a person answering.

    REPRODUCED BY REVIEW AS A REAL ACCUSATION, three times over. `_is_human` rejected
    any message with a `subtype`, and Slack attaches one to perfectly ordinary replies:
    `file_share` is "here's the list you asked for" — the most likely shape of the very
    reply being chased — `thread_broadcast` is the "also send to channel" checkbox, and
    `me_message` is `/me`. Each posted "she hasn't come back to me" about somebody who
    had answered.

    The rule is now a DENY list of subtypes that are genuinely not a person, so a
    subtype Slack adds next year counts as human. That asymmetry is deliberate:
    misreading a person as software produces a public accusation, misreading software
    as a person produces silence.
    """
    for subtype in ("file_share", "thread_broadcast", "me_message", ""):
        workdir = tmp_path / (subtype or "plain")
        workdir.mkdir()
        conn = _conn(workdir)
        _delivered_offer(conn, NOW - timedelta(hours=27))
        client = _Slack(human_reply_at="800.9", subtype=subtype)
        assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on", (
            f"a human reply with subtype {subtype!r} was read as software"
        )
        assert not client.posts
        conn.close()


def test_a_truncated_thread_is_unknown_not_silence(tmp_path: Path) -> None:
    """Slack returns replies OLDEST FIRST, so an unread page is where the answer is.

    The check requested one page of 200 and ignored `has_more`, which converted
    "unknown" into "positively established" — the single distinction this whole design
    rests on. Review reproduced it at a thread length of 201: verified silence, and the
    accusation posted.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    # The reply exists, but only on the third page.
    client = _Slack(human_reply_at="800.9", pages=3)

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert not client.posts
    assert client.pages_served >= 3, "it stopped reading before the reply"
    conn.close()


def test_a_thread_too_long_to_read_produces_no_accusation(tmp_path: Path) -> None:
    """Past the page budget the answer is None, which suppresses rather than accuses."""
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack(pages=nudge_silence.MAX_PAGES + 5)

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert not client.posts
    # Transient: a thread Grant could not finish reading must not burn the subject.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM followup_nudges WHERE subject_kind='offer_unanswered'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_a_reaction_is_an_answer(tmp_path: Path) -> None:
    """`grant.py` calls a reaction "the cheapest +1 there is"; this guard ignored them.

    So a card somebody had visibly acknowledged with an emoji could still be reported
    to a manager as untouched. The reactions were already inside the payload the code
    had fetched and discarded.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack(reactions=[{"name": "eyes", "users": [JOCELYN]}])

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert not client.posts
    conn.close()


def test_only_the_named_person_can_answer_for_themselves(tmp_path: Path) -> None:
    """A stranger's comment is not Jocelyn replying, and must not retire her follow-up.

    `replied_since` answered "did any human speak", which was used for a claim about
    ONE person. Review reproduced Nelly asking something unrelated in the thread and
    permanently suppressing Jocelyn's offer as `answered_since_offer` — erring safe on
    the accusation while silently destroying the feature's purpose.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack()
    # A different colleague speaks in the thread after the offer.
    client.conversations_replies = lambda **kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "messages": [
            {"bot_id": "B1", "ts": "700.1"},
            {"user": "U04ASV42UJD", "ts": "800.9"},
        ],
    }

    assert "nudged offer_unanswered" in nudges.run(client, conn, now=NOW)
    conn.close()


def test_an_escalation_the_addressee_cannot_see_is_not_sent(tmp_path: Path) -> None:
    """Naming a colleague publicly for an audience of nobody is the worst trade here.

    A mention does not notify somebody who is not in the conversation, so posting where
    the manager has not joined pays the full social cost, buys nothing, and reports
    success.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack(members=[JOCELYN])  # the manager is not in this channel

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert not client.posts
    conn.close()


def test_no_notifying_markup_survives_a_quoted_ask(tmp_path: Path) -> None:
    """Grant re-sends a colleague's words verbatim, and Slack stores mentions as markup.

    So a quoted `<!here>` pings the whole channel weeks later, and a quoted `<@U…>`
    pings a third party who is not the subject of the follow-up and whose opt-out
    nothing consults — no code path knows they are named inside a quotation. Rendering
    them as the words the reader originally saw is both the inert and the faithful
    choice.
    """
    conn = _conn(tmp_path)
    conn.execute(
        """INSERT INTO capability_asks
             (id,slack_user,audience,thread_ts,message_ts,asked_at,ask_text,
              capability,available_since,state,recorded_by,created_at)
           VALUES (1,?,?, '700.1','700.1',?,
                   '<!here> can grant email <@U01DPJVURHU> the list?',
                   'email_results',?,'open','test',?)""",
        (
            JOCELYN,
            CHANNEL,
            (NOW - timedelta(days=20)).isoformat(),
            (NOW - timedelta(days=1)).isoformat(),
            (NOW - timedelta(days=20)).isoformat(),
        ),
    )
    conn.commit()

    candidate = next(
        c
        for c in nudges.candidates(conn, NOW)
        if c.subject_kind == "capability_now_available"
    )
    live = nudges.build_message(candidate, "a", conn=conn)
    assert "<!here>" not in live, "a quoted broadcast would ping the whole channel"
    assert "<@U01DPJVURHU>" not in live, "a quoted mention would ping a third party"
    assert "@here" in live and "@Chase" in live, "the quote stopped reading as it did"
    # Grant's own addressing of the person it is writing TO still notifies them.
    assert f"<@{JOCELYN}>" in live
    conn.close()


# --------------------------------------------------------------- the silence reader


def test_a_bot_message_is_not_a_reply() -> None:
    """Three signals, because each has burned something already.

    An EMPTY `bot_id` must not count as a bot — the watchdog shipped exactly that,
    where a falsy id matched every message in the channel and read Grant's own silence
    as an answer.
    """

    class _Thread:
        def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
            """A thread of messages that were all posted by software, not people."""
            return {
                "messages": [
                    {"bot_id": "B1", "user": "U1", "ts": "900.1"},
                    {"app_id": "A1", "user": "U2", "ts": "900.2"},
                    {"subtype": "channel_join", "user": "U3", "ts": "900.3"},
                    {"bot_id": "", "user": "", "ts": "900.4"},
                ]
            }

    assert nudge_silence.replied_since(_Thread(), "C1", "800.1", "800.5") is False


def test_an_unreadable_thread_says_so_rather_than_guessing() -> None:
    """None is a real answer here, and it is the one that prevents a false claim."""

    class _Broken:
        def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
            """Fail the way a rate-limited or out-of-scope read actually fails."""
            raise RuntimeError("ratelimited")

    assert nudge_silence.replied_since(_Broken(), "C1", "800.1", "800.5") is None
    assert nudge_silence.replied_since(None, "C1", "800.1", "800.5") is None
