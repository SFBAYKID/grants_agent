"""A claimed lead goes quiet everywhere, and an unclaimed one does not.

WHY THIS FILE IS SEPARATE from `test_lead_claims.py`: that one pins the ledger, this
one pins the four candidate queries, the delivery veto and the nudge path — the places
a claim has to reach for the feature to mean anything. The distinction matters because
a green ledger with a live drip is the exact failure this was built to prevent: the
rep is told "recorded", and the card posts anyway.

EVERY SUPPRESSION TEST HERE CARRIES A CONTROL. A filter that hides everything passes a
suppression test just as well as a correct one, and it would take the whole product
down silently.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grant_watch import db, db_engagement, lead_claims
from grant_watch.campaign import contact_evidence, delivery, preparation
from grant_watch.campaign.contact_evidence import ContactFact
from grant_watch.slack import nudges
from grant_watch.slack.nudge_sources import NudgeCandidate

from contact_support import verified_contact_evidence
from drip_support import mk_lead

CHANNEL = "C0BSDPM2KPB"
NOW = datetime(2031, 5, 4, 17, 0, tzinfo=timezone.utc)


@pytest.fixture()
def two_leads(tmp_path: Path) -> tuple[sqlite3.Connection, int, int]:
    """One lead to claim and one to leave alone — the subject and its control."""
    conn = db.connect(tmp_path / "suppress.db")
    subject = mk_lead(conn, iid="A1", entity="Castle Rock School District 401")
    control = mk_lead(conn, iid="A2", entity="Tumwater School District")
    return conn, subject, control


def _take(conn: sqlite3.Connection, name: str, who: str = "U_KERRY") -> None:
    """Claim one organization by name, the way the tool does."""
    organization = next(org for org in lead_claims.resolve(conn, name))
    lead_claims.claim(
        conn,
        organization,
        slack_user=who,
        audience=CHANNEL,
        thread_ts="1.0",
        message_ts="1.0",
        claim_text=f"I'm taking {name}",
        now=NOW,
    )


def test_the_legacy_daily_card_stops_offering_a_claimed_lead(
    two_leads: tuple[sqlite3.Connection, int, int],
) -> None:
    """`nugget_candidates` — the fallback drip tier."""
    conn, subject, control = two_leads
    _take(conn, "Castle Rock")
    offered = {row["id"] for row in db_engagement.nugget_candidates(conn, CHANNEL)}
    assert subject not in offered
    assert control in offered, "the control lead must still be offered"


def test_the_rich_card_stops_offering_a_claimed_lead(
    two_leads: tuple[sqlite3.Connection, int, int],
) -> None:
    """`campaign.preparation._rows` — THE PATH THAT ACTUALLY POSTS.

    Separate from the test above on purpose. The two queries live in different files
    and were written months apart; a change that filters one and not the other looks
    entirely successful from the drip's side while the live path keeps posting.
    """
    conn, subject, control = two_leads
    _take(conn, "Castle Rock")
    offered = {row["id"] for row in preparation._rows(conn, CHANNEL, 50)}
    assert subject not in offered
    assert control in offered, "the control lead must still be offered"


def test_the_paid_enrichment_worker_will_not_buy_for_a_claimed_lead(
    two_leads: tuple[sqlite3.Connection, int, int],
) -> None:
    """A claimed lead must not merely go unposted; it must not be PAID FOR.

    ASSERTED ON `review_candidates`, NOT ON `preparable_lead_ids`, and the reason is
    worth keeping. `preparable_lead_ids` filters those reviews down to leads whose
    blockers preparation can actually close, and these bare fixture leads have none —
    so it returns an EMPTY tuple whatever the claim state, and "the claimed lead is
    absent" would have passed against nothing at all. Reviews are the shared input to
    both the rich card and the paid worker, and they are non-empty here, so this is
    the narrowest place the property is actually observable.
    """
    conn, subject, control = two_leads
    _take(conn, "Castle Rock")
    reviewed = {
        review.lead_id
        for review in preparation.review_candidates(
            conn, CHANNEL, frozenset(), limit=500, now=NOW
        )
    }
    assert reviewed, "an empty review set would make this assertion meaningless"
    assert subject not in reviewed
    assert control in reviewed, "the control lead must still be reviewed"


def _deliverable(conn: sqlite3.Connection, lead_id: int) -> tuple[int, str]:
    """Persist REAL verified contact evidence and return (event_id, evidence_id).

    Written through `contact_evidence.refresh` with an injected finder rather than by
    inserting a row with status='verified' by hand. The veto re-validates the stored
    evidence field by field, so a hand-written row would be rejected for its own
    reasons and the test would pass without ever reaching the claim check — which is
    exactly how the first version of this test managed to be vacuous.
    """
    fact = ContactFact(
        contact_type="named_direct",
        name="Dana Reed",
        title="Technology Director",
        email="dreed@castlerock.k12.wa.us",
        evidence_url="https://castlerock.k12.wa.us/staff",
        official_domain="castlerock.k12.wa.us",
        field_evidence=verified_contact_evidence(
            "Dana Reed",
            "dreed@castlerock.k12.wa.us",
            "https://castlerock.k12.wa.us/staff",
            title="Technology Director",
        ),
    )
    contact_evidence.refresh(
        conn, lead_id, finder_fn=lambda _lead: fact, now=NOW - timedelta(days=1)
    )
    row = conn.execute(
        "SELECT id FROM contact_evidence WHERE lead_id=? AND status='verified'",
        (lead_id,),
    ).fetchone()
    event_id = int(
        conn.execute(
            "SELECT current_event_id FROM leads WHERE id=?", (lead_id,)
        ).fetchone()[0]
    )
    return event_id, str(row["id"])


def test_a_claim_landing_after_preparation_still_cancels_the_post(
    two_leads: tuple[sqlite3.Connection, int, int],
) -> None:
    """The final veto, for a claim made INSIDE the tick.

    `review_candidates` runs at the top of a tick and the Slack call happens at the
    bottom. A rep claiming in between would otherwise be sent the card they had just
    said they were taking.

    The FIRST assertion is what makes the second one mean anything: this lead is
    genuinely deliverable until the claim lands.
    """
    conn, subject, _control = two_leads
    event_id, evidence_id = _deliverable(conn, subject)
    assert delivery._delivery_veto(conn, subject, event_id, evidence_id, NOW) is True
    _take(conn, "Castle Rock")
    assert delivery._delivery_veto(conn, subject, event_id, evidence_id, NOW) is False


def _card_candidate(kind: str, lead_id: int) -> NudgeCandidate:
    """A follow-up subject for a card that drew no reply — the Gobles shape."""
    return NudgeCandidate(
        subject_kind=kind,
        subject_id="48",
        audience=CHANNEL,
        target_slack="U01DFJWQQJ3",
        anchor_ts="1787853607.330079",
        stalled_at=NOW - timedelta(days=3),
        observed={
            "lead_id": lead_id,
            "entity_name": "Castle Rock School District 401",
            "lead_status": "new",
            "card_ts": "1787853607.330079",
            "tagged_slack": "",
        },
    )


@pytest.mark.parametrize("kind", ["card_unengaged", "card_escalated"])
def test_both_card_follow_ups_go_quiet_for_a_claimed_lead(
    two_leads: tuple[sqlite3.Connection, int, int], kind: str
) -> None:
    """BOTH kinds, from the same post row.

    Gating on the kind LABEL rather than the subject is a mistake this module has
    already made once: a lead marked not_relevant was suppressed in the channel and
    STILL reported to the manager as unanswered — the highest-consequence message in
    the system, saying something untrue about a colleague.
    """
    conn, subject, _control = two_leads
    _take(conn, "Castle Rock")
    assert (
        nudges.suppress_reason(conn, _card_candidate(kind, subject), NOW)
        == "lead_claimed"
    )


@pytest.mark.parametrize("kind", ["card_unengaged", "card_escalated"])
def test_an_unclaimed_card_is_still_followed_up(
    two_leads: tuple[sqlite3.Connection, int, int], kind: str
) -> None:
    """The control. A filter that silenced every card would pass the test above.

    Asserted as "not lead_claimed" rather than "not suppressed at all": an escalation
    is also gated on the manager being able to SEE the channel, which needs a Slack
    client this test has no business constructing. What matters here is that the CLAIM
    guard did not fire on an unclaimed lead.
    """
    conn, _subject, control = two_leads
    assert (
        nudges.suppress_reason(conn, _card_candidate(kind, control), NOW)
        != "lead_claimed"
    )


def test_the_claim_suppression_is_transient_so_release_can_undo_it() -> None:
    """`lead_claimed` must NEVER be written to the ledger.

    `run()` records a suppression only when the reason is permanent, and that row's
    uniqueness key retires the subject forever. A claim is the one reason here a human
    can reverse, so recording it would leave a released lead with its follow-up
    permanently destroyed — a one-way door disguised as a suppression.
    """
    assert "lead_claimed" not in nudges.PERMANENT_SUPPRESSIONS
    assert "lead_parked" in nudges.PERMANENT_SUPPRESSIONS, (
        "the neighbouring permanent reason must still be permanent, or this "
        "assertion proves nothing about the distinction"
    )


def test_releasing_hands_every_surface_back(
    two_leads: tuple[sqlite3.Connection, int, int],
) -> None:
    """A released lead returns to the card queues and to the follow-up path."""
    conn, subject, _control = two_leads
    _take(conn, "Castle Rock")
    organization = next(org for org in lead_claims.resolve(conn, "Castle Rock"))
    lead_claims.release(conn, organization, released_by="U_KERRY", now=NOW)
    assert subject in {
        row["id"] for row in db_engagement.nugget_candidates(conn, CHANNEL)
    }
    assert subject in {row["id"] for row in preparation._rows(conn, CHANNEL, 50)}
    assert (
        nudges.suppress_reason(conn, _card_candidate("card_unengaged", subject), NOW)
        != "lead_claimed"
    )
