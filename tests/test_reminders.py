"""Reminders, the opt-out that must always win, and email that cannot go astray.

The July failure these close: a rep asked Grant to email her a list and the thread
died, because nothing could outlive a conversation and nothing could send mail. The
risk introduced by fixing that is the mirror image — an agent that can schedule
messages and send email is an agent that can nag someone who asked it to stop, or mail
a school administrator without approval. These tests pin both directions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import capability_asks, db, reminders
from grant_watch.notify import resend_client

REP = "U0REP"
# Chase, from config/reps.json — the one identity certain to be on the roster.
ROSTERED_REP = "U01DPJVURHU"
OTHER = "U0OTHER"
CHANNEL = "C0TEST"
THREAD = "1700000000.000100"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A throwaway migrated database."""
    return db.connect(tmp_path / "r.db")


def _redirect(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Send bare db.connect() calls to the throwaway file.

    The tools open their own connection via db.connect(), whose default is bound at
    IMPORT time — patching db.DEFAULT_DB_PATH does nothing, which is how a test once
    wrote to the developer's real database (see tests/conftest.py).
    """
    real = db.connect

    def connect(db_path: object = None, *a: object, **k: object) -> object:
        """Open the throwaway file when no explicit path is given."""
        return real(path if db_path is None else db_path, *a, **k)

    monkeypatch.setattr(db, "connect", connect)


def _soon() -> datetime:
    """A due time comfortably in the future."""
    return datetime.now(timezone.utc) + timedelta(days=1)


def _make(conn: sqlite3.Connection, **kw: object) -> int:
    """Create one reminder with sensible defaults."""
    params: dict[str, object] = {
        "requested_by_slack": REP,
        "audience": CHANNEL,
        "thread_ts": THREAD,
        "subject": "the Texas RFPs",
        "due_at": _soon(),
    }
    params.update(kw)
    return reminders.create(conn, **params)  # type: ignore[arg-type]


def test_a_reminder_survives_the_conversation(tmp_path: Path) -> None:
    """The whole point: an ask that outlives the thread it was made in."""
    conn = _conn(tmp_path)
    reminder_id = _make(conn)
    mine = reminders.for_user(conn, REP)
    assert [item.reminder_id for item in mine] == [reminder_id]
    assert mine[0].subject == "the Texas RFPs"
    conn.close()


def test_stop_means_stop_everywhere(tmp_path: Path) -> None:
    """ "Stop reminding me" is not "cancel reminder #4".

    Someone who asks for quiet means every proactive channel, so an `all` opt-out has
    to satisfy the nudge worker's check too. Scoping it narrowly would leave Grant
    technically compliant and practically still nagging.
    """
    conn = _conn(tmp_path)
    _make(conn)
    reminders.set_optout(conn, REP, scope="all", channel=CHANNEL, thread_ts=THREAD)

    assert reminders.is_opted_out(conn, REP, scope="reminders") is True
    assert reminders.is_opted_out(conn, REP, scope="nudges") is True
    assert reminders.for_user(conn, REP) == [], "opting out left reminders running"
    assert reminders.is_opted_out(conn, OTHER) is False
    conn.close()


def test_a_new_reminder_is_refused_after_someone_opts_out(tmp_path: Path) -> None:
    """Cancelling what exists is not enough if the next ask re-arms it."""
    conn = _conn(tmp_path)
    reminders.set_optout(conn, REP)
    with pytest.raises(reminders.OptedOut):
        _make(conn)
    conn.close()


def test_someone_can_turn_follow_ups_back_on(tmp_path: Path) -> None:
    """An opt-out is a preference, not a punishment."""
    conn = _conn(tmp_path)
    reminders.set_optout(conn, REP)
    reminders.clear_optout(conn, REP)
    assert reminders.is_opted_out(conn, REP) is False
    assert _make(conn) > 0
    conn.close()


def test_only_the_owner_can_cancel_their_reminder(tmp_path: Path) -> None:
    """A shared channel means anyone can see it; that is not permission to change it."""
    conn = _conn(tmp_path)
    reminder_id = _make(conn)
    assert reminders.cancel(conn, reminder_id, OTHER) is False
    assert len(reminders.for_user(conn, REP)) == 1
    assert reminders.cancel(conn, reminder_id, REP) is True
    conn.close()


def test_one_occurrence_can_only_be_delivered_once(tmp_path: Path) -> None:
    """The reservation is the idempotency guarantee, not a bookkeeping detail.

    A worker that crashes between sending and recording will re-enter with the same
    reminder still due. If the second reserve succeeded, the rep gets the message
    twice — which is the failure mode that makes people mute a bot for good.
    """
    conn = _conn(tmp_path)
    _make(conn)
    reminder = reminders.due(conn, datetime.now(timezone.utc) + timedelta(days=2))[0]
    assert reminders.reserve(conn, reminder, "slack") is not None
    assert reminders.reserve(conn, reminder, "slack") is None, "double delivery"
    # A different channel for the same occurrence is a separate delivery and allowed.
    assert reminders.reserve(conn, reminder, "email") is not None
    conn.close()


def test_a_weekly_reminder_keeps_its_day_when_the_worker_runs_late(
    tmp_path: Path,
) -> None:
    """Stepping from `now` instead of the due time silently walks the weekday.

    A weekly reminder set for Friday, delivered at 09:05 because the worker ran late,
    would land the following Friday at 09:05 — and drift again every week until it
    was arriving on Tuesday. Stepping from the DUE time keeps it anchored.
    """
    conn = _conn(tmp_path)
    due = datetime.now(timezone.utc) - timedelta(hours=3)
    conn.execute(
        "INSERT INTO reminders (requested_by_slack,audience,thread_ts,subject,"
        "search_spec,cadence,deliver_via,next_due_at,state,created_at,updated_at) "
        "VALUES (?,?,?,'weekly thing','{}','weekly','slack',?,'active',?,?)",
        (REP, CHANNEL, THREAD, due.isoformat(), due.isoformat(), due.isoformat()),
    )
    conn.commit()
    reminder = reminders.due(conn, datetime.now(timezone.utc))[0]
    reminders.advance(conn, reminder)
    following = reminders.for_user(conn, REP)[0].next_due_at
    assert following == due + timedelta(days=7)
    assert following.weekday() == due.weekday()
    conn.close()


def test_a_one_off_reminder_retires_instead_of_repeating(tmp_path: Path) -> None:
    """`once` has to mean once even though the row is still there afterwards."""
    conn = _conn(tmp_path)
    _make(conn, cadence="once")
    reminder = reminders.due(conn, datetime.now(timezone.utc) + timedelta(days=2))[0]
    reminders.advance(conn, reminder)
    assert reminders.for_user(conn, REP) == []
    assert len(reminders.for_user(conn, REP, state="completed")) == 1
    conn.close()


def test_a_stored_search_cannot_smuggle_arguments_into_the_worker(
    tmp_path: Path,
) -> None:
    """THE INJECTION CASE. A frozen spec is model-written JSON that gets splatted
    into a real function call at delivery time, long after anyone reviewed it.

    Without the allowlist, a spec carrying `db_path` would point the reminder's search
    at another database, and `requester_slack` would let it act as somebody else. The
    thaw has to be narrower than the freeze.
    """
    hostile = {
        "state": "TX",
        "db_path": "/etc/passwd",
        "requester_slack": "U0ADMIN",
        "export": "google_sheet",
        "with_contacts": True,
        "limit": 9999,
    }
    safe = reminders.search_kwargs(hostile)
    assert set(safe) == {"state", "limit"}
    assert safe["state"] == "TX"
    assert safe["limit"] == reminders.MAX_REMINDER_ROWS, (
        "an unbounded limit got through"
    )


def test_a_recurring_reminder_stops_on_its_own(tmp_path: Path) -> None:
    """Something nobody ever engages with should expire, not run forever."""
    conn = _conn(tmp_path)
    reminder_id = _make(conn, cadence="daily")
    for index in range(reminders.MAX_OCCURRENCES):
        conn.execute(
            "INSERT INTO reminder_deliveries (reminder_id,occurrence_key,channel,"
            "state,reserved_at) VALUES (?,?,'slack','delivered',?)",
            (reminder_id, f"k{index}", "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    reminder = reminders.due(conn, datetime.now(timezone.utc) + timedelta(days=2))[0]
    reminders.advance(conn, reminder)
    assert reminders.for_user(conn, REP) == []
    conn.close()


# --- Email ------------------------------------------------------------------------


def test_grant_cannot_email_anyone_who_is_not_a_rostered_rep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guardrail that keeps a school administrator out of Grant's reach.

    Outreach to a prospect is Persequor's job and needs a human tap (Constitution rule
    10). This transport exists only to send a rep their own results, so an unknown
    Slack id has to refuse rather than fall back to anything.
    """
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "grant@example.test")
    monkeypatch.delenv("OUTREACH_TEST_EMAIL", raising=False)
    with pytest.raises(resend_client.RecipientNotAllowed):
        resend_client.send_to_rep("U-NOT-A-REP", "subject", "body")


def test_the_transport_exposes_no_way_to_name_a_recipient() -> None:
    """The guardrail is structural, so assert on the STRUCTURE.

    Every other test here could be satisfied by a validation check that a later
    refactor quietly loosens. This one fails if anyone ever adds an address
    parameter — the point is that steering Grant's mail is not expressible, not that
    it is currently rejected.
    """
    import inspect

    for name in ("send_to_rep", "recipient_for"):
        params = set(inspect.signature(getattr(resend_client, name)).parameters)
        assert not params & {"to", "email", "recipient", "address", "to_email"}, (
            f"{name} accepts a caller-supplied address; the roster is bypassable"
        )


def test_email_is_off_rather_than_broken_when_it_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key must mean an honest refusal, never a silent no-op that reads as sent."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("RESEND_FROM_EMAIL", "grant@example.test")
    assert resend_client.is_configured() is False
    # A REAL rostered id, so this proves the config gate rather than tripping the
    # recipient gate first — the recipient check runs earlier, deliberately, so that
    # an unknown address is refused whether or not email is switched on.
    with pytest.raises(resend_client.EmailNotConfigured):
        resend_client.send_to_rep(ROSTERED_REP, "subject", "body")


# --- Capability asks --------------------------------------------------------------


def _ask(conn: sqlite3.Connection, **kw: object) -> int | None:
    """Record one unmet ask with defaults."""
    params: dict[str, object] = {
        "slack_user": REP,
        "audience": CHANNEL,
        "thread_ts": THREAD,
        "message_ts": "1700000000.000200",
        "ask_text": "just email me the 29 texas ones",
        "capability": "email_results",
        "asked_at": "2026-07-24T18:00:00+00:00",
        "recorded_by": "test",
    }
    params.update(kw)
    return capability_asks.record(conn, **params)  # type: ignore[arg-type]


def test_an_unmet_ask_is_inert_until_the_capability_ships(tmp_path: Path) -> None:
    """Recording an ask must not by itself schedule a message to a colleague."""
    from grant_watch.slack import nudges

    conn = _conn(tmp_path)
    _ask(conn)
    found = nudges.candidates(conn, datetime.now(timezone.utc))
    assert [c for c in found if c.subject_kind == "capability_now_available"] == []

    assert capability_asks.mark_available(conn, "email_results") == 1
    found = nudges.candidates(conn, datetime.now(timezone.utc))
    reopened = [c for c in found if c.subject_kind == "capability_now_available"]
    assert len(reopened) == 1
    assert reopened[0].target_slack == REP
    assert reopened[0].anchor_ts == THREAD
    conn.close()


def test_a_months_old_ask_is_not_stale_once_the_capability_lands(
    tmp_path: Path,
) -> None:
    """The clock starts at the SHIP, not the ask — the whole design of this kind.

    Anchoring staleness to the ask date would make every historical ask permanently
    stale the moment it was recorded, and no bigger DROP_AFTER fixes that, because
    the gap grows by a day every day.
    """
    from grant_watch.slack import nudges

    conn = _conn(tmp_path)
    _ask(conn, asked_at="2026-01-05T18:00:00+00:00")
    capability_asks.mark_available(conn, "email_results")
    now = datetime.now(timezone.utc)
    candidate = [
        c
        for c in nudges.candidates(conn, now)
        if c.subject_kind == "capability_now_available"
    ][0]
    assert nudges.suppress_reason(conn, candidate, now) != "stale"
    conn.close()


def test_the_same_ask_is_only_recorded_once(tmp_path: Path) -> None:
    """Asking twice in one thread is one person wanting one thing."""
    conn = _conn(tmp_path)
    assert _ask(conn) is not None
    assert _ask(conn) is None
    conn.close()


def test_the_follow_up_quotes_the_person_verbatim(tmp_path: Path) -> None:
    """This message claims what a named colleague said, so it must not paraphrase."""
    from grant_watch.slack import nudges

    conn = _conn(tmp_path)
    _ask(conn)
    capability_asks.mark_available(conn, "email_results")
    candidate = [
        c
        for c in nudges.candidates(conn, datetime.now(timezone.utc))
        if c.subject_kind == "capability_now_available"
    ][0]
    text = nudges.build_message(candidate)
    assert "just email me the 29 texas ones" in text
    assert f"<@{REP}>" in text
    assert "couldn't do it then" in text
    conn.close()


# --- Truncated model output -------------------------------------------------------


def test_a_cut_off_answer_never_leaks_raw_json_to_a_rep() -> None:
    """A rep really received a Slack message starting `{"intent": "question"...`.

    When the model hits its token ceiling part-way through the required envelope, the
    JSON does not parse — and the fallback used to pass the raw text straight through
    as if it were prose. Internal scaffolding in a colleague's thread is a product
    defect; the half-sentence it ends on is worse, because it reads as a finished
    answer and is not one.
    """
    from grant_watch.slack import conversation

    truncated = (
        '```json\n{"intent": "question", "reply": "Both Excel files are done — '
        "Illinois Silver (18 rows) and Texas Silver (20 rows) are attached. I also "
        "tried building the campaign member previews since those campaigns are "
        "confirmed. Texas Grant 2026 matched 2 existing records but the o"
    )
    out = conversation._parse_final(truncated)

    assert '"intent"' not in out["reply"], "raw JSON envelope reached the user"
    assert '"reply"' not in out["reply"]
    assert "```json" not in out["reply"]
    # The useful prose survives...
    assert "Illinois Silver (18 rows)" in out["reply"]
    # ...but it stops at a finished sentence and says it was cut short.
    assert "but the o" not in out["reply"], "reply still ends mid-word"
    assert "ran out of room" in out["reply"]


def test_plain_prose_is_still_passed_through_untouched() -> None:
    """The salvage path must not swallow a model that simply answered in prose."""
    from grant_watch.slack import conversation

    out = conversation._parse_final("Found 12 leads in Texas. Want the list?")
    assert out["reply"] == "Found 12 leads in Texas. Want the list?"


def test_a_complete_envelope_is_unaffected() -> None:
    """The ordinary path stays exactly as it was."""
    from grant_watch.slack import conversation

    out = conversation._parse_final('{"intent": "question", "reply": "All good."}')
    assert out == {"intent": "question", "reply": "All good."}


def test_a_broken_promise_is_admitted_not_softened(tmp_path: Path) -> None:
    """Where Grant SAID the thing was handled, "I couldn't do it then" is not enough.

    Grant told a rep "I'll keep watching these states and flag new awards here as
    they land." It had no per-user watch and never contacted her again. Reopening
    that ask with the neutral capability line would be technically true and quietly
    misleading — it omits that she was told it was taken care of. Rule 1 applies to
    Grant's account of its own conduct, not only to lead data.
    """
    from grant_watch.slack import nudges

    conn = _conn(tmp_path)
    _ask(
        conn,
        capability="reminders",
        ask_text="Can you contact me on Slack for any grants awarded in Tx, AR, OK",
        correction="I told you I'd keep watching those states. That wasn't true.",
    )
    capability_asks.mark_available(conn, "reminders")
    candidate = [
        c
        for c in nudges.candidates(conn, datetime.now(timezone.utc))
        if c.subject_kind == "capability_now_available"
    ][0]
    text = nudges.build_message(candidate)

    assert "That wasn't true." in text
    assert "I couldn't do it then" not in text, (
        "the neutral line survived alongside the correction, softening the admission"
    )


# --- Choosing between stored contacts ---------------------------------------------


def test_the_better_of_two_stored_contacts_is_the_one_shown(tmp_path: Path) -> None:
    """A real production lead holds a Teacher AND an Assistant Superintendent.

    Each enrichment pass writes whoever LinkedIn returned that day, so a lead
    accumulates several linkedin_only rows. Picking the first one meant row order
    decided which human a rep was handed — and on that lead the better contact was
    the second row. This chooses between things Grant already verified; it must never
    merge two rows into one person.
    """
    from grant_watch.slack.contact_enrichment import _best_linkedin_contact

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('s','1','Mammoth Unified School District','CA','u')"
    )
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_linkedin_contact(
        conn, lead_id, "John Simeon", "Teacher", "https://linkedin.test/in/js"
    )
    db.save_linkedin_contact(
        conn,
        lead_id,
        "Lyle Tavernier",
        "Assistant Superintendent",
        "https://linkedin.test/in/lt",
    )
    best = _best_linkedin_contact(db.contacts_for_lead(conn, lead_id))
    assert best is not None
    assert best["name"] == "Lyle Tavernier", (
        "row order decided the contact instead of who a rep would actually call"
    )
    conn.close()


def test_an_untitled_contact_never_beats_a_titled_one(tmp_path: Path) -> None:
    """Two production rows have an empty title; those must not outrank a real one."""
    from grant_watch.slack.contact_enrichment import _best_linkedin_contact

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('s','1','Birmingham Community Charter','CA','u')"
    )
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_linkedin_contact(
        conn, lead_id, "Ari Bennett", "", "https://linkedin.test/in/ab"
    )
    db.save_linkedin_contact(
        conn, lead_id, "Dana Reyes", "Director of Technology", "https://li.test/in/dr"
    )
    # Insert an untitled row LAST, so recency alone would pick the wrong person.
    db.save_linkedin_contact(
        conn, lead_id, "Sam Later", "", "https://linkedin.test/in/sl"
    )
    best = _best_linkedin_contact(db.contacts_for_lead(conn, lead_id))
    assert best is not None and best["name"] == "Dana Reyes"
    conn.close()


# --- Model guidance must never reach a person -------------------------------------


def test_a_reminder_never_posts_model_coaching_to_a_human() -> None:
    """Caught by a LIVE playground test, not by any unit test I had written.

    A real reminder landed in Slack reading: "Nearby alternatives — without the state
    filter: 75 matches. Offer these to the user (with counts) and ask which to run;
    do not stop at a bare no-results answer." The second sentence is written FOR THE
    MODEL. The reminder worker posts tool text with no model in between, so it went
    to a person verbatim — the same defect class as leaking an internal identifier.
    """
    from grant_watch.presentation import for_human, for_model, model_note

    raw = (
        "No grants matched those filters.\nNearby alternatives — without the state "
        "filter: 75 matches."
        + model_note(
            " Offer these to the user (with counts) and ask which to run; do not "
            "stop at a bare no-results answer."
        )
    )
    human = for_human(raw)
    assert "Offer these to the user" not in human
    assert "do not stop at a bare" not in human
    assert "<model-note>" not in human
    # The FACT survives — the rep still learns there are 75 without the filter.
    assert "75 matches" in human
    assert "No grants matched those filters." in human

    # The model keeps the guidance, minus the delimiters.
    seen_by_model = for_model(raw)
    assert "Offer these to the user" in seen_by_model
    assert "<model-note>" not in seen_by_model


def test_the_live_search_output_carries_no_bare_coaching(tmp_path: Path) -> None:
    """End to end against the real search: a zero-result reminder is clean."""
    from grant_watch.presentation import for_human
    from grant_watch.slack.search import search_leads

    text, _ = search_leads(state="ZZ", grade="gold", db_path=tmp_path / "s.db")
    human = for_human(text)
    for coaching in (
        "Offer these to the user",
        "do not stop at a bare",
        "<model-note>",
    ):
        assert coaching not in human, f"model coaching reached a human surface: {human}"


def test_the_reminder_worker_itself_strips_the_coaching(tmp_path: Path) -> None:
    """Drive the WORKER, not the helper.

    The first version of this test exercised `for_human` directly and passed against
    a worker that had gone back to posting raw tool text — it proved the sanitiser
    worked while proving nothing about whether anyone called it. Mutation testing
    caught that. This drives `run()` end to end with a recording Slack stub and
    asserts on the text that would actually have been posted.
    """
    from grant_watch import reminder_worker

    posted: list[str] = []

    class _Client:
        """Records what would have been sent to Slack."""

        def chat_postMessage(self, **kwargs: object) -> dict[str, object]:
            """Capture the outgoing text."""
            posted.append(str(kwargs.get("text", "")))
            return {"ok": True, "ts": "1.1"}

    conn = _conn(tmp_path)
    # A search that returns nothing is what produced the leak in the playground.
    _make(
        conn,
        subject="gold leads in Texas",
        due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        search_spec={"state": "ZZ", "grade": "gold"},
    )
    reminder_worker.run(_Client(), conn)

    assert posted, "the reminder never went out"
    body = posted[0]
    for coaching in (
        "Offer these to the user",
        "do not stop at a bare",
        "<model-note>",
    ):
        assert coaching not in body, f"model coaching reached the rep: {body}"
    assert "you asked me to remind you about gold leads in Texas" in body
    conn.close()


def test_a_narrow_opt_out_does_not_claim_to_have_stopped_reminders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2, reproduced by the critic.

    Asking to stop only the nudges inserted a nudges-scoped opt-out, cancelled NO
    reminders — and returned "I've stopped following up with you, and cancelled the
    reminders you had running" regardless. The reminders then kept arriving. A
    confirmation has to be built from what the write actually did.
    """
    from grant_watch.slack import reminder_tools

    conn = _conn(tmp_path)
    _make(conn)
    conn.close()
    _redirect(monkeypatch, tmp_path / "r.db")

    said = reminder_tools.stop_followups({"scope": "nudges"}, REP, CHANNEL, THREAD)
    check = db.connect(tmp_path / "r.db")
    still_running = reminders.for_user(check, REP)

    assert still_running, "fixture is wrong — nothing was left to contradict"
    assert "cancelled" not in said.lower(), (
        f"claimed a cancellation that did not happen: {said}"
    )
    assert "reminders still run" in said
    check.close()


def test_a_full_opt_out_reports_the_real_number_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: when it DID cancel, say so, with the true count."""
    from grant_watch.slack import reminder_tools

    conn = _conn(tmp_path)
    _make(conn, subject="one")
    _make(conn, subject="two")
    conn.close()
    _redirect(monkeypatch, tmp_path / "r.db")

    said = reminder_tools.stop_followups({}, REP, CHANNEL, THREAD)
    assert "cancelled the 2" in said
    check = db.connect(tmp_path / "r.db")
    assert reminders.for_user(check, REP) == []
    check.close()


def test_a_spec_that_cannot_be_thawed_is_refused_when_it_is_set(
    tmp_path: Path,
) -> None:
    """C3, reproduced by the critic: one bad spec wedged the entire queue forever.

    `create` filtered keys but never type-checked values, and `search_kwargs` coerces
    at DELIVERY time inside a cron worker. A model writing {"amount_min": "$500,000"}
    for "over half a million" stored fine and then raised every tick — and because
    `due()` returns oldest-first, that row sat permanently at the head and NOTHING
    behind it ever went out again.
    """
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        _make(conn, search_spec={"state": "TX", "amount_min": "$500,000"})
    assert reminders.for_user(conn, REP) == []
    conn.close()


def test_one_broken_reminder_cannot_block_the_ones_behind_it(
    tmp_path: Path,
) -> None:
    """Belt and braces for C3: a row that predates the validation must not wedge it.

    `create` now refuses a bad spec, but a row written before that, or one whose
    search breaks later, still has to fail alone. The queue is oldest-first, so
    anything that raises at the head silently kills the whole feature.
    """
    from grant_watch import reminder_worker

    posted: list[str] = []

    class _Client:
        """Records outgoing Slack text."""

        def chat_postMessage(self, **kwargs: object) -> dict[str, object]:
            """Capture one post."""
            posted.append(str(kwargs.get("text", "")))
            return {"ok": True, "ts": "1.1"}

    conn = _conn(tmp_path)
    older = datetime.now(timezone.utc) - timedelta(hours=2)
    # Written straight to the table, bypassing `create` exactly as a legacy row would.
    conn.execute(
        "INSERT INTO reminders (requested_by_slack,audience,thread_ts,subject,"
        "search_spec,cadence,deliver_via,next_due_at,state,created_at,updated_at) "
        "VALUES (?,?,?,'poison','{\"amount_min\":\"$500,000\"}','once','slack',?,"
        "'active',?,?)",
        (REP, CHANNEL, THREAD, older.isoformat(), older.isoformat(), older.isoformat()),
    )
    conn.commit()
    _make(
        conn,
        subject="the good one",
        due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    # Two ticks: the poison row fails alone, the good one still gets delivered.
    reminder_worker.run(_Client(), conn)
    reminder_worker.run(_Client(), conn)

    assert any("the good one" in text for text in posted), (
        "a malformed reminder blocked every reminder behind it"
    )
    poisoned = conn.execute(
        "SELECT state,last_error FROM reminders WHERE subject='poison'"
    ).fetchone()
    assert poisoned["state"] == "failed"
    assert poisoned["last_error"], "the failure was not recorded anywhere"
    conn.close()


def test_the_email_surface_strips_coaching_exactly_like_the_slack_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The divergence that actually shipped, pinned so it cannot happen again.

    `email_results` ran `search_leads` directly while the reminder worker had already
    learned to strip model coaching and to broaden a zero-result search. A rep was
    emailed the literal instruction "Offer these to the user (with counts) and ask
    which to run", followed by nothing actionable. Both surfaces now share one
    renderer; this asserts on the EMAIL path specifically.
    """
    from grant_watch.slack import reminder_tools

    sent: list[tuple[str, str]] = []

    def fake_send(slack_user: object, subject: str, body: str, **_: object) -> object:
        """Capture what would have been mailed."""
        sent.append((subject, body))

        class _Outcome:
            recipient = "rep@example.test"
            email_id = "x"

        return _Outcome()

    monkeypatch.setattr(reminder_tools.resend_client, "is_configured", lambda: True)
    monkeypatch.setattr(reminder_tools.resend_client, "send_to_rep", fake_send)
    _redirect(monkeypatch, tmp_path / "e.db")

    reminder_tools.email_results({"search_spec": {"state": "ZZ"}}, ROSTERED_REP)
    assert sent, "nothing was sent"
    _subject, body = sent[0]
    for coaching in (
        "Offer these to the user",
        "do not stop at a bare",
        "<model-note>",
    ):
        assert coaching not in body, f"scaffolding was emailed to a rep: {body}"
    # And it does not dead-end.
    assert "campaign" in body.lower()


@pytest.mark.parametrize(
    ("raw", "must_survive", "label"),
    [
        (
            '```json\n{"intent":"question","reply":"Award confirmed. Verify it here: '
            "https://usaspending.gov/award/ABC123?tab=transactions and the spend",
            "https://usaspending.gov/award/ABC123?tab=transactions",
            "a verification link must not be cut at its query string",
        ),
        (
            '```json\n{"intent":"question","reply":"The award came from the U.S. '
            "Department of Justice and covers cameras",
            "U.S. Department of Justice",
            "an abbreviation is not a sentence end",
        ),
    ],
)
def test_truncation_salvage_does_not_mangle_what_it_keeps(
    raw: str, must_survive: str, label: str
) -> None:
    """Cutting at a bare "?" landed INSIDE a USASpending link.

    Grant's replies carry verification links, and a link truncated at its query
    string is a dead receipt for a dollar figure — worse than a long message,
    because it looks checkable and is not.
    """
    from grant_watch.slack import conversation

    reply = conversation._parse_final(raw)["reply"]
    assert must_survive in reply, f"{label}: got {reply!r}"


def test_an_empty_reply_is_not_reported_as_a_truncation() -> None:
    """A well-formed envelope with an empty reply was NOT cut off.

    Grant said "I got cut off before I could finish that one" when nothing had been,
    and threw away a real intent doing it.
    """
    from grant_watch.slack import conversation

    out = conversation._parse_final('{"intent":"snooze","reply":""}')
    assert "cut off" not in out["reply"].lower()
    assert out["intent"] == "snooze", "a real intent was discarded"


def test_a_reminder_that_failed_to_send_is_not_marked_completed(
    tmp_path: Path,
) -> None:
    """M3: a once-reminder was retired even when its Slack post failed.

    Grant says "Reminder #7 set for Friday 9am". The post fails with
    channel_not_found, `advance()` marks it completed, and the reminder dies having
    never been delivered — invisibly, because nothing recorded why.
    """
    from slack_sdk.errors import SlackApiError

    from grant_watch import reminder_worker

    class _Failing:
        """Slack rejecting every post."""

        def chat_postMessage(self, **_: object) -> dict[str, object]:
            """Fail the way Slack does."""
            raise SlackApiError("nope", {"error": "channel_not_found"})

    conn = _conn(tmp_path)
    _make(conn, due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    reminder_worker.run(_Failing(), conn)

    row = conn.execute("SELECT state,last_error FROM reminders").fetchone()
    assert row["state"] == "active", "a reminder that never sent was retired"
    assert row["last_error"], "the failure was not recorded anywhere"
    conn.close()


def test_only_one_email_can_leave_per_turn() -> None:
    """M2: the one tool whose side effect leaves the system had no per-turn key.

    Every other entry is keyed so a genuinely different request may run again. This
    one must not be: the agent loop runs several tool blocks per turn, so varying
    the subject line could put six real emails in a colleague's inbox from one
    sentence, and none of them can be recalled.
    """
    from grant_watch.slack import conversation

    first = conversation._single_execution_tool_key(
        "email_results", {"subject": "Texas", "search_spec": {"state": "TX"}}
    )
    second = conversation._single_execution_tool_key(
        "email_results", {"subject": "Texas again", "search_spec": {"state": "CA"}}
    )
    assert first == second != "", "different arguments would send a second email"


def test_a_refusal_records_the_ask_so_shipping_the_feature_reopens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger's whole justification was a writer that did not exist.

    `capability_asks.record` had exactly one caller — the manual seed command — so
    the migration's claim that "a row is written at the moment of refusal" was false,
    and the NEXT unmet ask would have been lost exactly as the last one was.
    """
    from grant_watch.slack import reminder_tools

    conn = _conn(tmp_path)
    conn.close()
    _redirect(monkeypatch, tmp_path / "r.db")
    monkeypatch.setattr(reminder_tools.resend_client, "is_configured", lambda: False)

    said = reminder_tools.email_results(
        {"subject": "the Texas ones", "search_spec": {"state": "TX"}},
        ROSTERED_REP,
        CHANNEL,
        THREAD,
    )
    assert said.startswith("ERROR:")

    check = db.connect(tmp_path / "r.db")
    row = check.execute("SELECT * FROM capability_asks").fetchone()
    assert row is not None, "the refusal recorded nothing; the next ask is lost"
    assert row["capability"] == "email_results"
    assert row["slack_user"] == ROSTERED_REP
    assert row["recorded_by"] == "refusal"
    assert row["available_since"] is None, "an unmet ask must start inert"
    check.close()


# --- Organization backfill ---------------------------------------------------------


def test_the_backfill_targets_leads_that_actually_need_it(tmp_path: Path) -> None:
    """A lead whose profile is already `found` must not be paid for twice.

    `enrich_org_profile` short-circuits on `found`, so including those would list
    work that does nothing. `unreachable` IS included, because that outcome is
    explicitly retryable.
    """
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    for index, (status, grade) in enumerate(
        [("found", "gold"), ("unreachable", "gold"), ("", "gold"), ("", "watch")]
    ):
        conn.execute(
            "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
            "lead_grade,org_profile_status,amount) "
            "VALUES ('s',?,?,'CA','u',?,?,?)",
            (str(index), f"Org {index}", grade, status or None, 1000 - index),
        )
    conn.commit()
    names = [row["entity_name"] for row in org_backfill.candidates(conn, grade="gold")]
    assert names == ["Org 1", "Org 2"], f"picked the wrong leads: {names}"
    conn.close()


def test_one_unreachable_site_does_not_end_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same wedge that let one bad reminder silence the whole queue.

    A corpus sweep meets broken sites constantly; if the first one aborts the run,
    the backfill never reaches the leads behind it.
    """
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    for index in range(3):
        conn.execute(
            "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
            "lead_grade,amount) VALUES ('s',?,?,'CA','u','gold',?)",
            (str(index), f"Org {index}", 1000 - index),
        )
    conn.commit()

    class _Profile:
        """A profile that found something."""

        street = "1 Main St"
        website = "https://x.test"
        phone = ""

    def flaky(_conn: object, lead_id: int, *_a: object, **_k: object) -> object:
        """Explode on the first lead, succeed afterwards."""
        if lead_id == 1:
            raise RuntimeError("site unreachable")
        return _Profile()

    monkeypatch.setattr(org_backfill, "enrich_org_profile", flaky)
    outcome = org_backfill.run(conn, grade="gold", dry_run=False)
    assert outcome.considered == 3
    assert outcome.failed == 1
    assert outcome.enriched == 2, "the sweep stopped at the first broken site"
    conn.close()


def test_the_backfill_spends_nothing_without_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each lead is a live scrape, so the default must be inert."""
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade,amount) VALUES ('s','1','Org','CA','u','gold',10)"
    )
    conn.commit()

    def explode(*_a: object, **_k: object) -> object:
        """Any call here means a dry run spent money."""
        raise AssertionError("a dry run performed a paid scrape")

    monkeypatch.setattr(org_backfill, "enrich_org_profile", explode)
    outcome = org_backfill.run(conn, grade="gold", dry_run=True)
    # `failed` is the load-bearing assertion. The sweep catches broad exceptions on
    # purpose, so it SWALLOWS the AssertionError above — asserting only on
    # considered/enriched passed against a version that had spent the money and
    # counted the resulting error. Caught by mutation testing, not by reading.
    assert (outcome.considered, outcome.enriched, outcome.failed) == (1, 0, 0)
    conn.close()
