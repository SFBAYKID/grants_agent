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

from grant_watch import reminders
from grant_watch.slack import nudge_promises, nudges

from nudge_helpers import (
    BRETT,
    CHANNEL,
    JOCELYN,
    MANAGER,
    NOW,
    _card,
    _conn,
    _delivered_offer,
    _Slack,
)


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
    # A link with WORDS on it. A naked permalink is ~130 characters of query string
    # under a one-line message, which reads as machine output in a channel of people.
    assert "<https://slack.example/archives/C0TEST/p1|See what I offered>" in text
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


def test_an_offer_made_in_a_dm_is_never_escalated(tmp_path: Path) -> None:
    """A DM has no manager in it, so an escalation there does nothing but leak.

    Production really does hold capability asks whose audience is a DM. Delivering
    there would fail silently — the manager is not in that conversation and would
    never see the message addressed to them — while repeating what somebody said in
    private back into that same private thread. Found by reading the real rows rather
    than by a test, which is why this one exists.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    conn.execute("UPDATE followup_nudges SET audience='D0BGW7EP3K5'")
    conn.commit()

    assert not [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "offer_unanswered"
    ]
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


def test_an_opted_out_owner_does_not_freeze_the_card_forever(tmp_path: Path) -> None:
    """The card named nobody, so the follow-up must not try to name them either.

    A SILENT PERMANENT STALL, found by review. `drip.py` drops the routing mention when
    the territory owner has opted out — the card still posts, because the lead belongs
    to the channel rather than to one person — but the follow-up recomputed `tagged`
    from territory WITHOUT that filter. So `card_unengaged` suppressed as `opted_out`,
    which is transient and writes no ledger row, and the escalation then waited forever
    for a `card_unengaged` row that could never exist. Both subjects sat due and
    undeliverable until they aged out, and nothing said so.

    Nothing about it was observable: no error, no suppression row, no message.
    """
    conn = _conn(tmp_path)
    # PA is Brett's territory and `usaspending:svpp` is a verified-state source.
    _card(conn, NOW - timedelta(days=2), lead_id=900)
    conn.execute("UPDATE leads SET state='PA' WHERE id=900")
    conn.commit()
    reminders.set_optout(conn, BRETT, scope="nudges")

    found = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_unengaged"
    ]
    assert found, "the card vanished from the queue entirely"
    assert found[0].target_slack == "", "it tried to name a rep the card never named"
    # And it is genuinely sendable rather than frozen behind a transient suppression.
    assert nudges.suppress_reason(conn, found[0], NOW, client=_Slack()) == ""
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


def test_a_preview_can_actually_see_an_escalation(tmp_path: Path) -> None:
    """The operator preview must show what is about to go out, not a false all-clear.

    `nudge --dry-run` used to withhold the Slack client entirely, as a structural
    guarantee that a preview could not post. Once escalations began establishing
    silence by READING the thread, that guarantee turned into a lie: the silence check
    failed closed as "could not verify silence" and the preview printed "nothing to
    follow up on" — reassuring, and wrong, from the one command run to find out what
    Grant is about to say about a colleague.

    Posting is prevented structurally instead: `run` returns at its dry-run branch
    before reserving or sending. This test pins BOTH halves — the escalation is
    visible, and nothing is written or posted.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack()

    outcome = nudges.run(client, conn, dry_run=True, now=NOW)
    assert "would nudge offer_unanswered" in outcome
    assert not client.posts
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM followup_nudges WHERE subject_kind='offer_unanswered'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_a_fresh_card_is_not_starved_behind_a_pile_of_old_asks(
    tmp_path: Path,
) -> None:
    """Kinds take turns, so the queue cannot stop reaching one of them.

    MEASURED, NOT IMAGINED. On the production queue the day this shipped there were 30
    live subjects and the North Palos card — the $500,000 lead Chase raised precisely
    because nobody engaged with it — sat at position 26. Two deliveries a day puts that
    ~13 days out against a 14-day staleness horizon, so it would very likely have gone
    stale unmentioned. Strict `priority_at` order across all kinds means every
    historical ask outranks every card forever, and cards are the kind that keeps
    arriving.

    The head of the queue must still be the person who has waited longest, so this
    pins both halves: oldest-first survives, and the card is reachable the same day.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=2))
    for index in range(20):
        asked = NOW - timedelta(days=30 + index)
        conn.execute(
            """INSERT INTO capability_asks
                 (id,slack_user,audience,thread_ts,message_ts,asked_at,ask_text,
                  capability,available_since,state,recorded_by,created_at)
               VALUES (?,?,?,?,?,?,'please do the thing','email_results',?,'open',
                       'test',?)""",
            (
                index + 1,
                JOCELYN,
                CHANNEL,
                f"70{index}.1",
                f"70{index}.1",
                asked.isoformat(),
                (NOW - timedelta(days=1)).isoformat(),
                asked.isoformat(),
            ),
        )
    conn.commit()

    order = [c.subject_kind for c in nudges.candidates(conn, NOW)]
    # The oldest ask still leads — the priority_at fix that moved Kerry 14th -> 0th
    # is not undone by taking turns.
    assert order[0] == "capability_now_available"
    # And the card is reachable inside the daily cap of 2 rather than 20 deep.
    assert "card_unengaged" in order[: nudges.MAX_NUDGES_PER_DAY], (
        f"a fresh card queued behind every old ask: {order[:6]}"
    )
    conn.close()


def test_the_best_lead_is_chased_first_not_the_oldest_card(tmp_path: Path) -> None:
    """A card has no person waiting on it, so age is the wrong priority for one.

    THIS IS THE BUG THE FIRST FIX SHIPPED. Round-robin across kinds alone moved North
    Palos from 26th to 29th of 30 — the card it existed to rescue came out LAST —
    because interleaving helps the oldest member of a small kind, and a freshly posted
    card is the newest member of the largest kind. The rotation was not the error; the
    sort key inside the kind was. `priority_at` means "how long has the PERSON
    waited", and applying it to a lead buries every new arrival behind older, worse
    ones.

    Cards are therefore ranked by the lead: tier, then money, then freshness — the
    grading CLAUDE.md already states. Here the fresh $500k gold card must beat a
    three-times-older nugget worth a tenth as much.
    """
    conn = _conn(tmp_path)
    _card(conn, NOW - timedelta(days=2), kind="rich_award", style="gold", lead_id=3100)
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,state,detail_url,"
        "amount,status) VALUES (900,'usaspending:svpp','old','OLD SMALL DISTRICT',"
        "'IL','u',50000,'new')"
    )
    conn.execute(
        "INSERT INTO posts (id,channel,ts,posted_at,lead_id,kind,style) "
        "VALUES (900,?,'900.1',?,900,'nugget','')",
        (CHANNEL, (NOW - timedelta(days=6)).isoformat()),
    )
    conn.commit()

    cards = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_unengaged"
    ]
    assert [c.observed["lead_id"] for c in cards] == [3100, 900], (
        "the older, smaller lead was chased ahead of the fresh $500k gold one"
    )
    conn.close()


def test_the_grade_is_read_from_the_column_that_holds_one(tmp_path: Path) -> None:
    """`posts.style` is not a grade vocabulary, and treating it as one broke ranking.

    The first version read `style or kind`. `kind` holds `rich_award`, `award-brief`,
    `nugget` — none of which are tiers — so whenever `style` was empty the tier was
    guaranteed unrecognised and sorted LAST. Measured on production: seven $500,000
    awards ranked below a $364,891 gold card. It failed safe, which is exactly why
    nobody would have noticed the grading had stopped working.

    Here the big award carries NO style and grade 'gold'; the small one carries the
    real production free-text style `award-brief` and the same grade. The half-million
    must win. (`award-brief` is a STYLE, not a kind — `posts.kind` carries a CHECK
    constraint that rejects it, which is how this test first failed.)
    """
    conn = _conn(tmp_path)
    for lead_id, grade, style, amount, days in (
        (3100, "gold", "", 500_000, 2),
        (900, "gold", "award-brief", 364_891, 3),
    ):
        conn.execute(
            "INSERT INTO leads (id,source,source_item_id,entity_name,state,"
            "detail_url,amount,status,lead_grade) VALUES (?,'usaspending:svpp',?,"
            "'DISTRICT','IL','u',?,'new',?)",
            (lead_id, f"x{lead_id}", amount, grade),
        )
        conn.execute(
            "INSERT INTO posts (id,channel,ts,posted_at,lead_id,kind,style) "
            "VALUES (?,?,?,?,?,'rich_award',?)",
            (
                lead_id,
                CHANNEL,
                f"8{lead_id}.1",
                (NOW - timedelta(days=days)).isoformat(),
                lead_id,
                style,
            ),
        )
    conn.commit()

    cards = [
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "card_unengaged"
    ]
    assert cards[0].observed["card_tier"] == "gold", (
        "an empty style fell through to a post kind and scored as unranked"
    )
    assert [c.observed["lead_id"] for c in cards] == [3100, 900]
    conn.close()


# ------------------------------------------------------------------ the rehearsal


def test_plain_mentions_render_a_name_and_notify_nobody(tmp_path: Path) -> None:
    """Chase's testing rule: "write at Anthony instead of actually tagging him".

    THE "@" GOES TOO, and that is the stricter reading on purpose. A bare `@Anthony`
    in message text creates no Slack mention and pushes no notification — but that is
    a fact about the link syntax, not about the person. Slack also notifies on
    HIGHLIGHT WORDS, and plenty of people keep their own first name in that list. A
    rehearsal whose entire purpose is that no colleague is disturbed should not rest
    on a technicality.

    It changes ONLY the rendering — the guards, caps and ledger writes are the live
    ones — so the rehearsal still exercises the real path.
    """
    conn = _conn(tmp_path)
    _delivered_offer(conn, NOW - timedelta(hours=27))
    client = _Slack()

    nudges.run(client, conn, now=NOW, plain_mentions=True)
    text = client.posts[0]["text"]
    assert "<@" not in text, "a rehearsal pinged a real person"
    assert "@" not in text, "an @ can still trip a highlight-word notification"
    assert "at Anthony" in text
    assert "at Jocelyn" in text
    conn.close()


def test_the_ordering_never_loses_or_duplicates_a_subject(tmp_path: Path) -> None:
    """`_fair_order` reorders; it must not be able to drop or clone work.

    The rotation pops from per-kind queues inside `while any(...)`, which is the shape
    that silently loses items if a queue is mutated wrongly, and hangs if one is never
    drained. A dropped subject is invisible — the follow-up simply never happens and
    nothing records that it did not — so this is checked as an invariant over
    randomised inputs rather than one hand-built case.

    Deterministic seed: a fuzz test that fails only sometimes is worse than none.
    """
    import random

    from grant_watch.slack import nudge_sources

    rng = random.Random("fair-order")
    kinds = list(nudges.NUDGE_SUBJECT_KINDS)
    for trial in range(200):
        items = [
            nudges.NudgeCandidate(
                rng.choice(kinds),
                str(index),
                CHANNEL,
                "",
                "1.1",
                NOW - timedelta(hours=rng.randint(0, 400)),
                {
                    "card_tier": rng.choice(["gold", "platinum", "", "award-brief"]),
                    "amount_usd": rng.randint(0, 900_000),
                },
            )
            for index in range(rng.randint(0, 40))
        ]
        ordered = nudge_sources._fair_order(
            sorted(items, key=lambda i: i.priority_at)
        )
        assert len(ordered) == len(items), f"trial {trial}: count changed"
        assert {(i.subject_kind, i.subject_id) for i in ordered} == {
            (i.subject_kind, i.subject_id) for i in items
        }, f"trial {trial}: a subject was dropped or duplicated"


# ------------------------------------------------------------------- the catalogue


def test_the_message_catalog_does_not_drift_from_the_code() -> None:
    """`docs/grant_message_catalog.md` claims to quote the code, so hold it to that.

    DOC DRIFT IS NOT COSMETIC HERE, and this project has the scar. The nudge band's
    own comment asserted one cron and CLAUDE.md asserted another; both were written
    from memory and neither matched the droplet, and the value recorded in the docs
    would have stranded 17.6% of delivery slots. The catalogue is the document a human
    reviews Grant's VOICE from — if it quotes sentences the code no longer produces,
    the review is of fiction.

    Cheap to keep true: every subject kind must be named, and a sampled fragment from
    each of three quoted templates must really appear in that kind's rendered message.
    """
    catalog = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "grant_message_catalog.md"
    ).read_text()

    for kind in nudges.NUDGE_SUBJECT_KINDS:
        assert kind in catalog, f"{kind} can be sent but is not in the catalogue"

    quoted = {
        "crm_preview_expired": "that approval timed out, so nothing got written",
        "thread_abandoned": "I never got you an answer on this one",
        "offer_unanswered": "Worth a poke from you, or shall I leave it?",
    }
    for kind, fragment in quoted.items():
        assert fragment in catalog, f"the sampled quote for {kind} left the catalogue"
        candidate = nudges.NudgeCandidate(
            kind,
            "1",
            CHANNEL,
            MANAGER,
            "1.1",
            NOW,
            {"silent_slack": JOCELYN, "capability": "campaign_load"},
        )
        assert fragment in nudges.build_message(candidate, "a"), (
            f"the catalogue quotes wording {kind} no longer produces"
        )


# ------------------------------------------------------------------ caps and queueing


def _tagged_card(
    conn: sqlite3.Connection, channel: str, lead_id: int, when: datetime
) -> None:
    """A card in one channel whose snapshot tags Brett."""
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,state,detail_url,"
        "amount,status,lead_grade) VALUES (?,'usaspending:svpp',?,'DISTRICT','PA',"
        "'u',500000,'new','gold')",
        (lead_id, f"x{lead_id}"),
    )
    conn.execute(
        "INSERT INTO posts (id,channel,ts,posted_at,lead_id,kind,style) "
        "VALUES (?,?,?,?,?,'rich_award','gold')",
        (lead_id, channel, f"9{lead_id}.1", when.isoformat(), lead_id),
    )
    conn.commit()


def test_one_person_gets_one_nudge_a_day_across_every_channel(
    tmp_path: Path,
) -> None:
    """The per-person cap is about a PHONE, and a phone has no idea which channel.

    Counting it per audience — as the daily cap correctly does — let production, the
    playground and a DM audience each spend their own allowance on the same human.
    Review reproduced four messages in one day with one rep nudged twice, which means
    a rehearsal in the playground could double a colleague's real notifications.
    """
    conn = _conn(tmp_path)
    _tagged_card(conn, CHANNEL, 900, NOW - timedelta(days=2))
    _tagged_card(conn, "C0OTHER", 901, NOW - timedelta(days=2))
    client = _Slack()

    assert "nudged card_unengaged" in nudges.run(
        client, conn, force=True, now=NOW, audience=CHANNEL
    )
    second = nudges.run(client, conn, force=True, now=NOW, audience="C0OTHER")

    delivered_to_brett = conn.execute(
        "SELECT COUNT(*) FROM followup_nudges "
        "WHERE target_slack=? AND state='delivered'",
        (BRETT,),
    ).fetchone()[0]
    assert delivered_to_brett == 1, f"{BRETT} was nudged twice in one day: {second}"
    conn.close()


def test_a_blocked_candidate_does_not_starve_the_one_behind_it(
    tmp_path: Path,
) -> None:
    """A pacing reason about ONE person must not stop every other subject.

    `run` returned on any pacing reason, so a head-of-queue card for a rep who was
    asleep blocked everything behind it on every tick — and told the operator that
    rep's clock was why nothing happened, while other subjects were fully sendable.
    """
    conn = _conn(tmp_path)
    # Kerry is America/New_York; at 16:00 Pacific it is 19:00 for her, outside her
    # hours, while the shared window is still open.
    late = datetime(2026, 8, 12, 23, 0, tzinfo=timezone.utc)
    # WA is Kerry's territory and `usaspending:svpp` is a verified-state source, so
    # the card tags her exactly as production would, with no snapshot needed.
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,state,detail_url,"
        "amount,status,lead_grade) VALUES (900,'usaspending:svpp','k','DISTRICT',"
        "'WA','u',500000,'new','gold')"
    )
    conn.execute(
        "INSERT INTO posts (id,channel,ts,posted_at,lead_id,kind,style) "
        "VALUES (900,?,'900.1',?,900,'rich_award','gold')",
        (CHANNEL, (late - timedelta(days=5)).isoformat()),
    )
    # And a completely unrelated, fully sendable subject behind it.
    conn.execute(
        """INSERT INTO crm_actions
             (id,action_type,workspace,channel,thread_ts,requested_by,state,
              payload_json,payload_hash,nonce_hash,expires_at,created_at,updated_at)
           VALUES ('act-9','add_campaign_members','T1',?,'100.1',?,'ready',
                   '{}','h','n',?,?,?)""",
        (
            CHANNEL,
            BRETT,
            (late - timedelta(hours=6)).isoformat(),
            (late - timedelta(hours=6)).isoformat(),
            (late - timedelta(hours=6)).isoformat(),
        ),
    )
    conn.commit()

    outcome = nudges.run(_Slack(), conn, force=True, now=late)
    assert "crm_preview_expired" in outcome, (
        f"one sleeping rep starved the whole queue: {outcome}"
    )
    conn.close()


def test_the_batch_nudge_counts_only_the_orgs_that_are_stuck(
    tmp_path: Path,
) -> None:
    """"Still stuck on 14 orgs" when 13 of 14 matched is a figure with no source.

    `blocked_resolution` is set when ANY item is unresolved, so reporting the batch
    size asserted something its own data contradicts — to a named rep, which is rule 1.
    """
    conn = _conn(tmp_path)
    stalled = (NOW - timedelta(days=3)).isoformat()
    # Before the items, which carry a foreign key to it.
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,state,detail_url,"
        "amount,status) VALUES (900,'s','x','ORG','CA','u',1,'new')"
    )
    conn.execute(
        """INSERT INTO crm_campaign_batches
             (id,workspace,channel,thread_ts,requested_by,query_version,state,
              completion_mode,expected_source_row_count,stored_source_row_count,
              source_row_count,unique_org_count,selection_hash,writer_org_id,
              writer_is_sandbox,writer_host,created_at,updated_at)
           VALUES ('b1','T1',?,'500.1',?,'v1','blocked_resolution','all',
                   14,14,14,14,'h','org',0,'host',?,?)""",
        (CHANNEL, BRETT, stalled, stalled),
    )
    conn.execute(
        """INSERT INTO crm_campaign_batch_targets
             (id,batch_id,campaign_id,campaign_name,campaign_link,state_code,
              grades_json,expected_source_row_count,stored_source_row_count,
              source_row_count,unique_org_count,selection_hash,approved_org_count,
              approved_selection_hash,completion_mode,state,created_at,updated_at)
           VALUES ('t1','b1','c1','CA','link','CA','["gold"]',14,14,14,14,'h',0,'',
                   'all','blocked_resolution',?,?)""",
        (stalled, stalled),
    )
    for index in range(14):
        conn.execute(
            """INSERT INTO crm_campaign_batch_items
                 (id,target_id,canonical_entity_key,representative_lead_id,
                  source_lead_ids_json,grades_json,entity_name,state_code,item_hash,
                  resolution_state)
               VALUES (?,'t1',?,900,'[900]','["gold"]','ORG','CA','h',?)""",
            (
                f"i{index}",
                f"key{index}",
                "existing_record" if index < 13 else "ambiguous",
            ),
        )
    conn.commit()

    candidate = next(
        c for c in nudges.candidates(conn, NOW) if c.subject_kind == "crm_batch_blocked"
    )
    text = nudges.build_message(candidate, "a", conn=conn)
    assert "1 org I can't match" in text, text
    assert "14" not in text
    conn.close()
