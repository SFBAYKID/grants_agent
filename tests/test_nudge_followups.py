"""Chasing what nobody answered — and refusing to when Grant cannot prove silence.

TWO CLAIMS ARE BEING TESTED, and they pull in opposite directions. The feature exists
because good leads and live offers were dying in silence: North Palos District 117 went
out with $500,000 of verified award money and drew nothing, and Grant told Jocelyn it
could build her campaign and nobody ever noticed she had not replied. So the worker has
to be persistent.

But the sentence it says — "X has not come back to me" — is the only claim in the whole
system that is ABOUT one colleague and addressed TO another, in a channel they both
read. Persistence bought at the cost of getting that wrong is not worth having. So most
of what follows tests the refusals: no escalation on a thread Grant could not read, none
about somebody who asked for quiet, none before the rep has had their own turn, and none
that promises something the outreach path would not actually do.

Every test builds its own temp database and closes it; nothing here touches the real
one (conftest fails any test that tries).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from grant_watch import db, reminders
from grant_watch.slack import nudge_promises, nudge_silence, nudges

CHANNEL = "C0TEST"
MANAGER = "U01DFJWQQJ3"  # Anthony, the one roster row carrying `manager: true`
JOCELYN = "U06RXJKRXSR"
BRETT = "U08C1NBH875"
# A Wednesday inside the business window (11:00 Pacific).
NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


class _Slack:
    """Slack stand-in whose thread contents the test controls.

    `human_reply_at` is the ts of a human message in the thread; None means the thread
    holds only Grant's own post, which is verified silence. `explode` makes every read
    fail, which is how an outage or a missing scope actually presents.
    """

    def __init__(
        self, *, human_reply_at: str | None = None, explode: bool = False
    ) -> None:
        """Record posts and serve a thread the test has described."""
        self.posts: list[dict[str, Any]] = []
        self.human_reply_at = human_reply_at
        self.explode = explode

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        """Record one post."""
        self.posts.append(kwargs)
        return {"ok": True, "ts": "999.1"}

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        """The thread, or a failure if this test is exercising an unreadable one."""
        if self.explode:
            raise RuntimeError("slack is down")
        messages: list[dict[str, Any]] = [{"bot_id": "B1", "ts": "700.1"}]
        if self.human_reply_at:
            messages.append({"user": JOCELYN, "ts": self.human_reply_at})
        return {"ok": True, "messages": messages}

    def chat_getPermalink(self, **kwargs: Any) -> dict[str, Any]:
        """A working link, so the escalation can point at what it is about."""
        return {"ok": True, "permalink": "https://slack.example/archives/C0TEST/p1"}


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database of its own."""
    return db.connect(tmp_path / "followups.db")


def _delivered_offer(conn: sqlite3.Connection, when: datetime) -> str:
    """A `capability_now_available` follow-up that was delivered to Jocelyn.

    This is the exact shape of the real row behind Chase's case: Grant posted "I can
    build that campaign now — want me to?" into the channel thread where she had asked
    on 23 July, and the ledger recorded the delivery.
    """
    conn.execute(
        """INSERT INTO followup_nudges
             (id,subject_kind,subject_id,audience,target_slack,anchor_ts,
              policy_version,due_at,drop_after,state,observed_json,delivery_key,
              reserved_at,delivered_at,slack_ts,variant)
           VALUES ('offer-1','capability_now_available','5',?,?,'700.1',
                   'nudge-v1',?,?,'delivered',
                   '{"capability":"campaign_load","ask_text":"I want them both, the gold and the silver leads please","asked_on":"23 July"}',
                   'k1',?,?,'800.5','b')""",
        (
            CHANNEL,
            JOCELYN,
            when.isoformat(),
            (when + timedelta(days=14)).isoformat(),
            when.isoformat(),
            when.isoformat(),
        ),
    )
    conn.commit()
    return "offer-1"


def _card(
    conn: sqlite3.Connection,
    when: datetime,
    *,
    kind: str = "rich_award",
    style: str = "gold",
    lead_id: int = 900,
) -> None:
    """A posted lead card of one tier, which nobody engaged with."""
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,state,detail_url,"
        "amount,status) VALUES (?,'usaspending:svpp',?,"
        "'NORTH PALOS SCHOOL DISTRICT 117','IL','u',500000,'new')",
        (lead_id, f"x{lead_id}"),
    )
    conn.execute(
        "INSERT INTO posts (id,channel,ts,posted_at,lead_id,kind,style) "
        "VALUES (?,?,?,?,?,?,?)",
        (lead_id, CHANNEL, f"8{lead_id}.1", when.isoformat(), lead_id, kind, style),
    )
    conn.commit()


# --------------------------------------------------------------- the unanswered offer


def test_an_offer_nobody_answered_reaches_the_manager_in_the_channel(
    tmp_path: Path,
) -> None:
    """Chase's case, end to end.

    Before this, delivery WAS completion: `capability_now_available` closes the ask the
    moment it posts, and the one-shot key retires the subject forever. So an offer that
    went unanswered was indistinguishable from one that had been dealt with.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack()

    assert "nudged offer_unanswered" in nudges.run(client, conn, now=NOW)
    posted = client.posts[0]
    assert posted["channel"] == CHANNEL
    # Top-level in the channel, not buried in the thread nobody read.
    assert "thread_ts" not in posted
    text = posted["text"]
    assert f"<@{MANAGER}>" in text  # the manager is being told
    assert f"<@{JOCELYN}>" in text  # about this person
    # It names the SPECIFIC offer, not "something I offered". Both wordings say
    # "that campaign"; only the grammar around it differs ("offered to build" vs
    # "come back about building"), so the assertion pins the fact, not the phrasing.
    assert "that campaign" in text
    assert "https://slack.example" in text  # with a link to the actual words
    conn.close()


def test_it_waits_the_night_out_before_saying_anything(tmp_path: Path) -> None:
    """Twenty-six hours, so somebody who was simply busy is not reported at teatime."""
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=20))
    assert nudges.run(_Slack(), conn, now=NOW) == "skip: nothing to follow up on"
    conn.close()


def test_a_reply_retires_the_subject_permanently(tmp_path: Path) -> None:
    """Answering is as permanent as any other resolution — never ask again.

    The reply is found in SLACK, not in `slack_event_receipts`. That table only holds
    events Grant woke for and processed, and its own docstring says it undercounts; a
    reply Grant never saw would otherwise read as "she ignored you".
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack(human_reply_at="800.9")  # after the offer's own ts of 800.5

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert not client.posts
    row = conn.execute(
        "SELECT state,suppress_reason FROM followup_nudges "
        "WHERE subject_kind='offer_unanswered'"
    ).fetchone()
    assert row["state"] == "suppressed"
    assert row["suppress_reason"] == "answered_since_offer"
    conn.close()


def test_a_reply_before_the_offer_does_not_count_as_answering_it(
    tmp_path: Path,
) -> None:
    """The clock starts at the OFFER, not at the top of the thread.

    The offer is a reply INTO an existing conversation — the one where she made the
    original request — so the thread is guaranteed to contain her earlier messages. A
    check that looked at the whole thread would find them and conclude she had answered
    something she was never shown.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack(human_reply_at="700.9")  # BEFORE the offer's ts of 800.5

    assert "nudged offer_unanswered" in nudges.run(client, conn, now=NOW)
    conn.close()


def test_an_unreadable_thread_never_produces_an_accusation(tmp_path: Path) -> None:
    """Fail closed, and do not burn the subject while failing.

    Two properties in one test because they are two halves of the same decision: an
    outage must not cause a false claim, AND must not silently destroy the follow-up so
    that the true claim can never be made once Slack recovers.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    broken = _Slack(explode=True)

    assert nudges.run(broken, conn, now=NOW) == "skip: nothing to follow up on"
    assert not broken.posts
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM followup_nudges WHERE subject_kind='offer_unanswered'"
        ).fetchone()[0]
        == 0
    ), "an outage burned the subject; it can never be raised again"

    # And once Slack is readable again, the same subject goes out normally.
    assert "nudged offer_unanswered" in nudges.run(_Slack(), conn, now=NOW)
    conn.close()


def test_one_escalation_per_offer_ever(tmp_path: Path) -> None:
    """The one-shot rule covers the new kind too — chasing a chase is nagging."""
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack()

    assert "nudged offer_unanswered" in nudges.run(client, conn, now=NOW)
    later = nudges.run(client, conn, now=NOW + timedelta(days=1))
    assert "offer_unanswered" not in later
    assert len(client.posts) == 1
    conn.close()


# ----------------------------------------------------------------------- the opt-out


def test_quiet_means_quiet_about_you_as_well_as_to_you(tmp_path: Path) -> None:
    """"Stop following up" has to cover being TALKED ABOUT, not just being addressed.

    The escalation's `target_slack` is the MANAGER, so the ordinary opt-out check asks
    whether the manager wants quiet — and would happily announce "Jocelyn never
    answered" in a public channel about the one person who had explicitly asked Grant
    to leave her alone. Someone who opts out of follow-ups is opting out of being
    followed up about.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    reminders.set_optout(conn, JOCELYN, scope="all", note="asked Grant to stop")
    client = _Slack()

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert not client.posts
    conn.close()


def test_an_opt_out_does_not_permanently_burn_the_subject(tmp_path: Path) -> None:
    """An opt-out is reversible, so it must not retire the subject forever.

    Chase turned one back on the same week ("turn them back on"), which is exactly the
    case that would otherwise have silently destroyed the pending queue.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    reminders.set_optout(conn, JOCELYN, scope="nudges")
    nudges.run(_Slack(), conn, now=NOW)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM followup_nudges WHERE subject_kind='offer_unanswered'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


# ------------------------------------------------------------- what may be promised


def test_the_offer_matches_what_the_outreach_path_will_actually_accept(
    tmp_path: Path,
) -> None:
    """The promise and the capability are pinned to the same predicate.

    `grant._request_outreach` selects its contact with `contact_status == 'verified'`.
    If a follow-up offered a named draft on the strength of a vendor or LinkedIn row,
    the offer would read as specific and the acceptance would land in the branch that
    says no contact could be verified — a promise broken by the very next message.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=2))

    # A ZoomInfo row carries an email but is not page-verified: no named promise.
    conn.execute(
        "INSERT INTO contacts (lead_id,name,title,email,contact_status) "
        "VALUES (900,'Sean Joyce','Director of Technology',"
        "'sjoyce@npd117.net','vendor_licensed')"
    )
    conn.commit()
    assert nudge_promises.best_offer(conn, 900).kind == "find_email"

    conn.execute("UPDATE contacts SET contact_status='verified' WHERE lead_id=900")
    conn.commit()
    offer = nudge_promises.best_offer(conn, 900)
    assert offer.kind == "draft_intro"
    assert offer.contact_name == "Sean"
    # It offers a DRAFT for approval. Grant does not send prospect email — a human
    # approves and Persequor sends — and the database structurally cannot know whether
    # anything was ever delivered, so no wording here may claim it was.
    assert "approve" in offer.question
    for forbidden in ("I'll email", "I emailed", "send an email to", "I will send"):
        assert forbidden not in offer.question
    conn.close()


def test_with_no_contact_it_promises_only_what_it_can_always_do(
    tmp_path: Path,
) -> None:
    """No contact on file means no name in the sentence, and a narrower offer."""
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=2))
    offer = nudge_promises.best_offer(conn, 900)
    assert offer.kind == "find_contact"
    assert offer.contact_name == ""
    conn.close()


def test_the_escalation_carries_the_real_offer_for_that_lead(tmp_path: Path) -> None:
    """The manager is told something actionable, and it is true for THIS lead."""
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=2))
    conn.execute(
        "INSERT INTO contacts (lead_id,name,title,email,contact_status) "
        "VALUES (900,'Sean Joyce','Director of Technology',"
        "'sjoyce@npd117.net','verified')"
    )
    conn.commit()
    escalation = next(
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_escalated"
    )
    text = nudges.build_message(escalation, "a", conn=conn)
    assert "Sean" in text and "approve" in text
    assert "$500,000" in text
    conn.close()


# ------------------------------------------------------------------ every card tier


def test_every_tier_of_card_gets_chased(tmp_path: Path) -> None:
    """Gold, platinum and silver alike — Chase named all three.

    The tier says how good the lead is, not whether silence about it matters, so the
    source query deliberately carries no filter on kind or style. This pins that: a
    later "only chase gold" optimisation has to delete a failing test to land.
    """
    for index, (kind, style) in enumerate(
        [
            ("rich_award", "platinum"),
            ("rich_award", "gold"),
            ("rfp", "silver"),
            ("nugget", ""),
        ]
    ):
        tier_dir = tmp_path / f"tier{index}"
        tier_dir.mkdir()
        conn = _conn(tier_dir)
        _card(conn, NOW - timedelta(days=2), kind=kind, style=style, lead_id=900)
        found = [
            c
            for c in nudges.candidates(conn, NOW)
            if c.subject_kind == "card_unengaged"
        ]
        assert found, f"a {style or kind} card was never followed up"
        conn.close()


# ------------------------------------------------------------------ the rehearsal


def test_plain_mentions_render_a_name_and_notify_nobody(tmp_path: Path) -> None:
    """Chase's testing rule: "write at Anthony instead of actually tagging him".

    It changes ONLY the rendering — the guards, caps and ledger writes are the live
    ones — so a playground rehearsal exercises the real path without putting a phone
    notification on a colleague's lock screen.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack()

    nudges.run(client, conn, now=NOW, plain_mentions=True)
    text = client.posts[0]["text"]
    assert "<@" not in text, "a rehearsal pinged a real person"
    assert "@Anthony" in text
    assert "@Jocelyn" in text
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
