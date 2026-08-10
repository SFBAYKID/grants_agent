"""Proactive follow-ups: what Grant may claim, and how often it may say it.

Two failure modes matter more than anything else here. The first is a false claim in
a team channel — silence in Slack is not evidence a rep did nothing, so a nudge may
only report what Grant observed in its own records. The second is nagging: a rep who
parked something deliberately must not be asked about it every morning.

Both are enforced structurally rather than by wording discipline. One nudge per
subject is a UNIQUE constraint, and every claim is re-verified inside the same call
that reserves the send, so anything resolved while queued produces silence.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


from grant_watch import db
from grant_watch.slack import nudges

CHANNEL = "C0TEST"
REP = "U0REP"
# A Wednesday inside the business window (11:00 Pacific).
NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


class _Client:
    """Slack stand-in recording posts, or failing the way Slack does."""

    def __init__(self, error: str = "") -> None:
        """Record posts, or raise the named Slack error on every send."""
        self.posts: list[dict[str, Any]] = []
        self.error = error

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        """Record one post or raise a SlackApiError with the configured code."""
        if self.error:
            from slack_sdk.errors import SlackApiError

            raise SlackApiError(self.error, {"error": self.error})
        self.posts.append(kwargs)
        return {"ok": True, "ts": "999.1"}


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database."""
    return db.connect(tmp_path / "n.db")


def _expired_preview(conn: sqlite3.Connection, when: datetime) -> None:
    """Insert a ready preview whose approval window has already lapsed."""
    conn.execute(
        """INSERT INTO crm_actions
             (id,action_type,workspace,channel,thread_ts,requested_by,state,
              payload_json,payload_hash,nonce_hash,expires_at,created_at,updated_at)
           VALUES ('act-1','add_campaign_members','T1',?,'100.1',?,'ready',
                   '{}','h','n',?,?,?)""",
        (CHANNEL, REP, when.isoformat(), when.isoformat(), when.isoformat()),
    )
    conn.commit()


def test_an_abandoned_preview_is_followed_up_once_and_only_once(
    tmp_path: Path,
) -> None:
    """The one-shot rule: a second tick must not produce a second nudge."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    client = _Client()

    first = nudges.run(client, conn, now=NOW)
    assert "nudged crm_preview_expired" in first
    assert len(client.posts) == 1
    assert client.posts[0]["thread_ts"] == "100.1"
    assert client.posts[0]["channel"] == CHANNEL

    second = nudges.run(client, conn, now=NOW + timedelta(days=1))
    assert "nothing to follow up" in second
    assert len(client.posts) == 1
    conn.close()


def test_the_message_reports_what_grant_saw_and_never_asserts_inaction(
    tmp_path: Path,
) -> None:
    """Grant cannot see a phone call, so it must not imply none happened."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    client = _Client()
    nudges.run(client, conn, now=NOW)
    text = str(client.posts[0]["text"]).lower()
    assert "timed out before anyone hit the button" in text
    assert "nothing got written" in text
    for forbidden in ("you didn't", "you did not", "you never", "you forgot"):
        assert forbidden not in text
    conn.close()


def test_a_subject_that_resolved_while_queued_produces_silence(
    tmp_path: Path,
) -> None:
    """Re-verification happens at send time, not when the candidate was found."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    conn.execute("UPDATE crm_actions SET state='complete' WHERE id='act-1'")
    conn.commit()
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, now=NOW)
    assert client.posts == []
    conn.close()


def test_something_too_old_is_dropped_rather_than_mentioned_late(
    tmp_path: Path,
) -> None:
    """A nudge about three-week-old work is noise; it is recorded, not posted."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=20))
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, now=NOW)
    assert client.posts == []
    row = conn.execute("SELECT state,suppress_reason FROM followup_nudges").fetchone()
    assert row["state"] == "suppressed"
    assert row["suppress_reason"] == "stale"
    conn.close()


def test_a_dry_run_writes_absolutely_nothing(tmp_path: Path) -> None:
    """A command that posts to a team channel must be inert without --execute."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    client = _Client()
    outcome = nudges.run(client, conn, dry_run=True, now=NOW)
    assert outcome.startswith("[dry-run] would nudge")
    assert client.posts == []
    assert conn.execute("SELECT COUNT(*) FROM followup_nudges").fetchone()[0] == 0
    conn.close()


def test_the_reservation_is_committed_before_slack_is_called(
    tmp_path: Path,
) -> None:
    """A crash mid-send must not let the next tick post a duplicate."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))

    class _Exploding:
        """Fails after the reservation, the way a network drop would."""

        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            """Confirm a row already exists, then fail."""
            count = conn.execute(
                "SELECT COUNT(*) FROM followup_nudges WHERE state='reserved'"
            ).fetchone()[0]
            assert count == 1, "the send must not precede its reservation"
            raise RuntimeError("connection reset")

    outcome = nudges.run(_Exploding(), conn, now=NOW)
    assert "ambiguous" in outcome
    row = conn.execute("SELECT state FROM followup_nudges").fetchone()
    # Ambiguity is preserved, never retried: a duplicate nudge is worse than none.
    assert row["state"] == "unknown"
    assert "nothing to follow up" in nudges.run(_Client(), conn, now=NOW)
    conn.close()


def test_a_deleted_thread_is_suppressed_and_never_retried(tmp_path: Path) -> None:
    """thread_not_found is permanent; retrying it forever helps nobody."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(hours=6))
    outcome = nudges.run(_Client(error="thread_not_found"), conn, now=NOW)
    assert "thread_not_found" in outcome
    assert conn.execute("SELECT state FROM followup_nudges").fetchone()[0] == (
        "suppressed"
    )
    conn.close()


def test_only_one_person_is_nudged_per_day(tmp_path: Path) -> None:
    """A nudge is a phone notification; two in a day for one rep is pestering."""
    conn = _conn(tmp_path)
    for index in (1, 2):
        conn.execute(
            """INSERT INTO crm_actions
                 (id,action_type,workspace,channel,thread_ts,requested_by,state,
                  payload_json,payload_hash,nonce_hash,expires_at,created_at,updated_at)
               VALUES (?,'add_campaign_members','T1',?,?,?,'ready','{}','h','n',?,?,?)""",
            (
                f"act-{index}",
                CHANNEL,
                f"10{index}.1",
                REP,
                (NOW - timedelta(hours=6)).isoformat(),
                (NOW - timedelta(hours=6)).isoformat(),
                (NOW - timedelta(hours=6)).isoformat(),
            ),
        )
    conn.commit()
    client = _Client()
    assert "nudged" in nudges.run(client, conn, now=NOW)
    second = nudges.run(client, conn, now=NOW + timedelta(minutes=5))
    assert "skip:" in second
    assert len(client.posts) == 1
    conn.close()


def test_nothing_is_sent_outside_business_hours(tmp_path: Path) -> None:
    """03:00 Pacific is not when a teammate should be pinged about a campaign."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=1))
    client = _Client()
    night = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)  # 03:00 PT
    assert "skip:" in nudges.run(client, conn, now=night)
    assert client.posts == []
    conn.close()


def test_a_parked_lead_is_not_chased(tmp_path: Path) -> None:
    """A rep who marked a lead dead has answered the question already."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,detail_url,status) "
        "VALUES (1,'s','1','Hoxie School District','u','dead')"
    )
    conn.execute(
        "INSERT INTO posts (id,kind,lead_id,channel,ts,posted_at) "
        "VALUES (1,'nugget',1,?,'50.1',?)",
        (CHANNEL, (NOW - timedelta(days=5)).isoformat()),
    )
    conn.commit()
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, now=NOW)
    assert client.posts == []
    assert (
        conn.execute("SELECT suppress_reason FROM followup_nudges").fetchone()[0]
        == "lead_parked"
    )
    conn.close()


def test_an_engaged_card_is_not_chased(tmp_path: Path) -> None:
    """Someone already replied; there is nothing to follow up on."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,detail_url,status) "
        "VALUES (1,'s','1','Hoxie School District','u','new')"
    )
    conn.execute(
        "INSERT INTO posts (id,kind,lead_id,channel,ts,posted_at) "
        "VALUES (1,'nugget',1,?,'50.1',?)",
        (CHANNEL, (NOW - timedelta(days=5)).isoformat()),
    )
    conn.execute(
        "INSERT INTO engagement (post_id,slack_user,kind,at) VALUES (1,?,'reply',?)",
        (REP, NOW.isoformat()),
    )
    conn.commit()
    assert "nothing to follow up" in nudges.run(_Client(), conn, now=NOW)
    conn.close()


def test_a_card_nudge_names_no_one(tmp_path: Path) -> None:
    """A card belongs to the channel, so nobody is singled out in public."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,detail_url,status) "
        "VALUES (1,'s','1','HOXIE SCHOOL DISTRICT NO 46','u','new')"
    )
    conn.execute(
        "INSERT INTO posts (id,kind,lead_id,channel,ts,posted_at) "
        "VALUES (1,'nugget',1,?,'50.1',?)",
        (CHANNEL, (NOW - timedelta(days=5)).isoformat()),
    )
    conn.commit()
    client = _Client()
    assert "nudged card_unengaged" in nudges.run(client, conn, now=NOW)
    text = str(client.posts[0]["text"])
    assert "<@" not in text
    assert "only what I can see" in text
    conn.close()


def test_every_declared_subject_kind_has_a_message(tmp_path: Path) -> None:
    """A subject with no wording would post an empty or generic nudge."""
    for kind in nudges.NUDGE_SUBJECT_KINDS:
        candidate = nudges.NudgeCandidate(
            subject_kind=kind,
            subject_id="x",
            audience=CHANNEL,
            target_slack=REP,
            anchor_ts="1.1",
            stalled_at=NOW,
            observed={"entity_name": "Test District", "organizations": 3},
        )
        message = nudges.build_message(candidate)
        assert len(message) > 40, kind
        assert "?" in message, kind


def test_force_skips_the_window_but_not_the_one_shot_rule(tmp_path: Path) -> None:
    """The operator override sends now — it does not make Grant nag.

    force exists so this path can be exercised outside a weekday. It deliberately
    skips ONLY the business-hours window: the one-shot rule, the suppression
    re-checks and the daily caps are what stop a nudge being wrong or being
    repetitive, and an override that skipped those would be testing something other
    than the real behaviour.
    """
    conn = _conn(tmp_path)
    saturday = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)  # 11:00 PT, a Saturday
    _expired_preview(conn, saturday - timedelta(hours=6))
    client = _Client()

    assert "skip:" in nudges.run(client, conn, now=saturday)
    assert client.posts == []

    assert "nudged" in nudges.run(client, conn, force=True, now=saturday)
    assert len(client.posts) == 1

    # Still one-shot, even forced.
    assert "nothing to follow up" in nudges.run(
        client, conn, force=True, now=saturday + timedelta(hours=5)
    )
    assert len(client.posts) == 1
    conn.close()


def test_force_does_not_bypass_suppression(tmp_path: Path) -> None:
    """A subject that resolved must stay silent however hard an operator pushes."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=1))
    conn.execute("UPDATE crm_actions SET state='complete' WHERE id='act-1'")
    conn.commit()
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, force=True, now=NOW)
    assert client.posts == []
    conn.close()


def test_the_eligible_window_is_wide_enough_to_drain_a_backlog(tmp_path: Path) -> None:
    """A subject abandoned last week is still worth one question.

    DROP_AFTER used to be 5 days, which — with a 2-day grace and a one-a-day cap —
    left an eligible window only three days wide. Everything that piled up while the
    feature was switched off aged out before the feature could look at it: measured
    on production the day it shipped, 28 of 36 due subjects were already unreachable.
    The worker would have said "nothing to follow up on" with a fortnight of
    abandoned previews sitting in front of it.
    """
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=8))
    client = _Client()
    assert "nudged" in nudges.run(client, conn, force=True, now=NOW)
    assert len(client.posts) == 1
    conn.close()


def test_something_genuinely_ancient_is_still_dropped(tmp_path: Path) -> None:
    """Widening the window must not turn the worker into an archaeologist."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=40))
    client = _Client()
    assert "nothing to follow up" in nudges.run(client, conn, force=True, now=NOW)
    assert client.posts == []
    assert (
        conn.execute("SELECT suppress_reason FROM followup_nudges").fetchone()[0]
        == "stale"
    )
    conn.close()


def test_a_slack_outage_does_not_permanently_destroy_the_backlog(
    tmp_path: Path,
) -> None:
    """THE BUG THIS CLOSES, found by measuring the real queue against production.

    `_record` writes the row whose uniqueness key retires a subject FOREVER under this
    policy version, and the worker was writing it for EVERY suppression — including
    `channel_guard_active`, which is a Slack outage that clears on its own. So a
    single run during an outage silently and permanently burned every pending
    follow-up in that channel: 22 subjects, in the measured case, with nothing in the
    output to say it had happened.

    A transient reason must leave no trace, so the subject is still there when Slack
    comes back.
    """
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=1))
    db.set_channel_guard(
        conn,
        CHANNEL,
        "backoff",
        "ratelimited",
        available_at=(NOW + timedelta(hours=2)).isoformat(),
    )
    client = _Client()

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert client.posts == []
    burned = conn.execute("SELECT COUNT(*) FROM followup_nudges").fetchone()[0]
    assert burned == 0, (
        "a transient Slack outage wrote a permanent suppression and destroyed the "
        "subject; it can never be nudged again"
    )

    # With the guard gone, the very same subject is still reachable.
    conn.execute("DELETE FROM notification_outbox WHERE lead_id IS NULL")
    conn.commit()
    assert "nudged" in nudges.run(client, conn, now=NOW)
    assert len(client.posts) == 1
    conn.close()


def test_a_permanent_reason_is_still_recorded_once_and_retires_the_subject(
    tmp_path: Path,
) -> None:
    """The other half: not recording transient reasons must not stop recording real
    ones, or every stale subject would be re-examined forever."""
    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=90))  # far past DROP_AFTER
    assert nudges.run(_Client(), conn, now=NOW) == "skip: nothing to follow up on"
    row = conn.execute("SELECT state,suppress_reason FROM followup_nudges").fetchone()
    assert (row["state"], row["suppress_reason"]) == ("suppressed", "stale")
    conn.close()


def test_someone_who_opted_out_is_not_nudged_and_is_not_burned(
    tmp_path: Path,
) -> None:
    """Opting out silences Grant without destroying the work.

    `opted_out` is transient by the same argument as the outage: the person can ask
    for follow-ups back, and if the subject had been retired meanwhile there would be
    nothing left to tell them about.
    """
    from grant_watch import reminders

    conn = _conn(tmp_path)
    _expired_preview(conn, NOW - timedelta(days=1))
    reminders.set_optout(conn, REP, scope="all")
    client = _Client()

    assert nudges.run(client, conn, now=NOW) == "skip: nothing to follow up on"
    assert client.posts == []
    assert conn.execute("SELECT COUNT(*) FROM followup_nudges").fetchone()[0] == 0

    reminders.clear_optout(conn, REP)
    assert "nudged" in nudges.run(client, conn, now=NOW)
    conn.close()


def _card(
    conn: sqlite3.Connection,
    when: datetime,
    *,
    state: str = "PA",
    source: str = "usaspending:svpp",
    amount: int = 500000,
) -> None:
    """A posted card for a lead in one state, from one source."""
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,state,detail_url,"
        "amount,status) VALUES (900,?,'x','HOXIE SCHOOL DISTRICT',?,'u',?,'new')",
        (source, state, amount),
    )
    conn.execute(
        "INSERT INTO posts (id,channel,ts,posted_at,lead_id,kind) "
        "VALUES (900,?,'800.1',?,900,'nugget')",
        (CHANNEL, when.isoformat()),
    )
    conn.commit()


def test_a_card_follow_up_asks_the_rep_the_card_actually_tagged(
    tmp_path: Path,
) -> None:
    """A tagged card is one person's to answer, so the follow-up names them.

    It previously addressed the channel, which is why a card that pinged a rep and
    got no answer produced a follow-up nobody owned.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=3))  # PA -> Brett; usaspending:svpp is verified
    found = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_unengaged"
    ]
    assert found and found[0].target_slack == "U08C1NBH875"
    assert "<@U08C1NBH875>" in nudges.build_message(found[0])
    conn.close()


def test_an_inferred_state_still_cannot_name_a_human(tmp_path: Path) -> None:
    """The follow-up must use the SAME gate the card used.

    `owner_for_state` alone would tag a rep on a card that went out UNTAGGED, because
    a source that only inferred the state may never tag anybody. That would invent a
    claim of ownership the card itself never made — and the aggregator really does
    read "1600 Pennsylvania Avenue" as PA.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=3), source="rfp-aggregator")
    found = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_unengaged"
    ]
    assert found and found[0].target_slack == ""
    assert "<@" not in nudges.build_message(found[0])
    # And with nobody tagged there is nobody to escalate about.
    assert not [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_escalated"
    ]
    conn.close()


def test_the_manager_hears_about_it_only_after_the_rep_has_had_a_fair_run(
    tmp_path: Path,
) -> None:
    """Escalating on day one turns a follow-up into telling on a colleague."""
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=2))
    assert not [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_escalated"
    ], "the manager was told before the rep had a chance to answer"

    conn.execute("DELETE FROM posts")
    conn.execute("DELETE FROM leads")
    _card(conn, NOW - timedelta(days=5))
    escalations = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_escalated"
    ]
    assert len(escalations) == 1
    escalation = escalations[0]
    # It is a DM to the manager, not a reply in the channel the rep can see.
    assert escalation.audience == "U01DFJWQQJ3"
    assert escalation.subject_kind in nudges.DM_KINDS
    text = nudges.build_message(escalation)
    assert "<@U01DFJWQQJ3>" in text
    assert "$500,000" in text
    assert "<@U08C1NBH875>" in text
    # It reports Grant's own view and does not assert what the rep did.
    assert "nothing's come back here" in text
    for accusation in ("didn't follow up", "never followed up", "ignored"):
        assert accusation not in text.lower()
    conn.close()


def test_an_abandoned_conversation_is_only_reopened_if_nobody_came_back(
    tmp_path: Path,
) -> None:
    """The signal is Grant's own failed turn, not a judgement about the human.

    And if the person sent anything afterwards they returned under their own steam,
    so there is nothing to apologise for.
    """
    conn = _conn(tmp_path)
    stalled = NOW - timedelta(days=2)
    conn.execute(
        "INSERT INTO slack_event_receipts (event_id,workspace,channel,thread_ts,"
        "slack_user,state,received_at) VALUES ('ev1','T1',?,'700.1',?, "
        "'needs_reconciliation',?)",
        (CHANNEL, REP, stalled.isoformat()),
    )
    conn.commit()
    found = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "thread_abandoned"
    ]
    assert len(found) == 1
    assert found[0].target_slack == REP
    text = nudges.build_message(found[0])
    assert "never got you a proper answer" in text
    assert f"<@{REP}>" in text

    # The rep posts again in the same thread: they came back, so drop it.
    conn.execute(
        "INSERT INTO slack_event_receipts (event_id,workspace,channel,thread_ts,"
        "slack_user,state,received_at) VALUES ('ev2','T1',?,'700.1',?,'complete',?)",
        (CHANNEL, REP, (stalled + timedelta(hours=1)).isoformat()),
    )
    conn.commit()
    assert not [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "thread_abandoned"
    ]
    conn.close()


def test_opting_out_silences_the_manager_escalation_too(tmp_path: Path) -> None:
    """A manager who asked for quiet gets quiet, like anybody else."""
    from grant_watch import reminders

    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=5))
    reminders.set_optout(conn, "U01DFJWQQJ3", scope="all")
    escalation = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_escalated"
    ][0]
    assert nudges.suppress_reason(conn, escalation, NOW) == "opted_out"
    conn.close()


def test_a_parked_lead_never_escalates_to_the_manager(tmp_path: Path) -> None:
    """C1, reproduced by the critic and executed before it was fixed.

    The channel nudge was correctly suppressed for a lead the rep had marked
    not_relevant — and the manager was DM'd about it anyway, because the guard was
    written against the KIND LABEL rather than the subject. That is the highest-
    consequence message in the system saying something untrue about a colleague.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=6))
    conn.execute("UPDATE leads SET status='not_relevant' WHERE id=900")
    conn.commit()
    for candidate in nudges.candidates(conn, NOW):
        if candidate.subject_kind in {"card_unengaged", "card_escalated"}:
            assert nudges.suppress_reason(conn, candidate, NOW) == "lead_parked", (
                f"{candidate.subject_kind} was not suppressed for a parked lead"
            )
    client = _Client()
    nudges.run(client, conn, now=NOW)
    assert client.posts == [], "a parked lead produced a message"
    conn.close()


def test_a_clicked_button_counts_as_engagement(tmp_path: Path) -> None:
    """A button click writes nowhere near `engagement`.

    The rich-card buttons write `rich_card_actions` through a snapshot keyed on
    lead_id, and an approval writes `crm_actions` in the card's thread. Reading only
    `engagement` meant a card somebody had actually acted on was chased anyway.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=6))
    conn.execute(
        "INSERT INTO rich_card_snapshots (id,policy_version,audience,dedup_key,"
        "lead_id,tier,entity_name,entity_kind,entity_kind_provenance,"
        "routing_reason,fallback_text,render_inputs_json,created_at) "
        "VALUES (1,'v1',?,'k',900,'gold','H','school','nces','territory','t','{}',?)",
        (CHANNEL, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO rich_card_actions (id,snapshot_id,action,nonce,requester_slack,"
        "state,created_at,updated_at) VALUES (1,1,'draft','n',?,'accepted',?,?)",
        (REP, NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    for candidate in nudges.candidates(conn, NOW):
        if candidate.subject_kind in {"card_unengaged", "card_escalated"}:
            assert (
                nudges.suppress_reason(conn, candidate, NOW) == "engaged_since_queued"
            )
    conn.close()


def test_the_escalation_names_the_rep_the_card_actually_routed_to(
    tmp_path: Path,
) -> None:
    """A rich card routes by Salesforce ownership FIRST, territory last.

    Recomputing the rep from the state alone therefore names a different person for
    any relationship-routed card — and telling a manager "this went to X and nothing
    came back" about somebody who was never asked is the worst thing this feature
    can do. The card records who it tagged; that value wins.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=5))  # state PA -> territory says Brett
    conn.execute(
        "INSERT INTO rich_card_snapshots (id,policy_version,audience,dedup_key,"
        "lead_id,tier,entity_name,entity_kind,entity_kind_provenance,"
        "routing_reason,fallback_text,render_inputs_json,slack_user_id,created_at) "
        "VALUES (7,'v1',?,'k2',900,'gold','H','school','nces','sf_account_owner',"
        "'t','{}',?,?)",
        (CHANNEL, "U04ASV42UJD", NOW.isoformat()),
    )
    conn.execute("UPDATE posts SET snapshot_id=7 WHERE id=900")
    conn.commit()

    escalation = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_escalated"
    ][0]
    text = nudges.build_message(escalation)
    assert "<@U04ASV42UJD>" in text, "named the territory rep, not the routed one"
    assert "<@U08C1NBH875>" not in text, "named someone who was never asked"
    conn.close()
