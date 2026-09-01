"""What Grant SAYS when a rep takes a lead — the surface a human actually reads.

The suppression tests prove a claimed lead goes quiet. These prove the other half:
that Grant never guesses which organization, never implies Salesforce changed, and
never repeats a colleague's words in a way that pings a bystander or leaks a DM.

REAL ROSTER IDS ON PURPOSE. `config/reps.json` is the reviewed identity file and the
gate fails closed against it, so a test using a made-up `U_KERRY` would exercise the
refusal path while looking like it exercised the success path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grant_watch import db, lead_claims
from grant_watch.slack import claim_tools
from grant_watch.slack.search_presentation import claimed_phrases

KERRY = "U01E908206M"  # on the roster, renders as "Kerry"
NELLY = "U04ASV42UJD"
STRANGER = "U0STRANGER1"  # deliberately absent from config/reps.json
CHANNEL = "C0BSDPM2KPB"
DM = "D0BGW7EP3K5"


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """A database holding two same-named organizations in different states."""
    path = tmp_path / "claims.db"
    conn = db.connect(path)
    rows = [
        ("GOBLES PUBLIC SCHOOLS", "MI"),
        ("Gobles Public Schools", "MI"),  # the duplicate row shape
        ("GOBLES ELEMENTARY", "ME"),
        ("TUMWATER SCHOOL DISTRICT", "WA"),
    ]
    for index, (name, state) in enumerate(rows, start=1):
        conn.execute(
            """INSERT INTO leads
                 (id,source,source_item_id,lead_grade,entity_name,state,
                  canonical_entity_key,status)
               VALUES (?,?,?,'gold',?,?,?,'new')""",
            (
                index,
                "t",
                f"i{index}",
                name,
                state,
                db.canonical_entity_key(name, state),
            ),
        )
    conn.commit()
    # `claim_tools.db` IS the db module, so patching through it patches globally —
    # capture the real function first or the lambda calls itself.
    opener = db.connect
    monkeypatch.setattr(claim_tools.db, "connect", lambda *a, **k: opener(path))
    return conn


def _take(
    who: str = KERRY,
    name: str = "Gobles Public Schools",
    said: str = "I'm taking Gobles Public Schools",
    channel: str = CHANNEL,
    **args: object,
) -> str:
    """Run the tool the way the dispatcher does."""
    return claim_tools.claim_lead(
        {"name": name, **args}, who, channel, "1756742520.000100", said
    )


def _live(conn: sqlite3.Connection) -> dict[int, lead_claims.Claim]:
    """Every claim currently held, so a test can assert nothing was written."""
    return lead_claims.live_claims(conn, [1, 2, 3, 4])


def test_a_named_organization_is_recorded_and_echoed_back(
    wired: sqlite3.Connection,
) -> None:
    """Success names the exact lead ids, so a wrong claim is visible immediately."""
    reply = _take(name="Gobles Public Schools", state="MI")
    assert "#1" in reply and "#2" in reply
    assert set(_live(wired)) == {1, 2}


def test_the_reply_never_implies_salesforce_changed(
    wired: sqlite3.Connection,
) -> None:
    """Grant cannot set an Owner on any Salesforce record, and must say so.

    The rep who prompted this feature had already been told, correctly, that
    ownership was between him and Salesforce. A claim must not blur that back.
    """
    reply = _take().lower()
    assert "not salesforce" in reply
    for lie in ("owner", "assigned it in salesforce", "updated salesforce"):
        assert f"salesforce {lie}" not in reply


def test_an_ambiguous_name_writes_nothing_and_asks(
    wired: sqlite3.Connection,
) -> None:
    """Two states matching means Grant does not know which was meant.

    Michigan and Maine both hold a "Gobles" — this is the real shape, not a
    hypothetical: Salesforce showed exactly that pair.
    """
    reply = _take(name="Gobles")
    assert "MI" in reply and "ME" in reply
    assert _live(wired) == {}, "an ambiguous claim must write nothing at all"


def test_a_pronoun_is_refused(wired: sqlite3.Connection) -> None:
    """ "I'll take that one" claims nothing. A claim holds until somebody undoes it."""
    reply = _take(name="it")
    assert reply.startswith("ERROR")
    assert _live(wired) == {}


def test_an_unknown_organization_is_refused_honestly(
    wired: sqlite3.Connection,
) -> None:
    """No lead on file is said plainly, never invented and never silently ignored."""
    reply = _take(name="Springfield Unified")
    assert "no lead on file" in reply.lower()
    assert _live(wired) == {}


def test_a_second_rep_is_refused_and_the_holder_is_named(
    wired: sqlite3.Connection,
) -> None:
    """Never a silent transfer, and never a raw Slack id in the sentence."""
    _take(KERRY)
    reply = _take(NELLY, said="I'll take Gobles")
    assert "Kerry" in reply
    assert KERRY not in reply, "a display name, never the id"
    assert {claim.slack_user for claim in _live(wired).values()} == {KERRY}


def test_a_quoted_claim_can_never_ping_a_bystander(
    wired: sqlite3.Connection,
) -> None:
    """`claim_text` is raw Slack wire text, and it is quoted to a third party later.

    A stored `<!here>` re-fires for the whole channel and a stored `<@U…>` notifies
    somebody who is not part of the exchange — a bill this repo has already paid once.
    """
    _take(
        KERRY,
        said="<!here> I'm taking Gobles, ask <@U01DFJWQQJ3|anthony> <!subteam^S123>",
    )
    reply = _take(NELLY, said="mine")
    assert "<!" not in reply and "<@" not in reply
    assert "Kerry" in reply


def test_a_claim_made_in_a_dm_is_never_quoted_into_a_channel(
    wired: sqlite3.Connection,
) -> None:
    """A third party gets the FACT and the DATE, never the private words.

    `nudge_sources` already refuses to carry a DM's contents into a channel, with the
    reason written out: it would report somebody's private conversation with Grant.
    """
    _take(KERRY, said="I'm taking Gobles, don't tell the others", channel=DM)
    reply = _take(NELLY, said="mine")
    assert "Kerry" in reply, "who holds it is still reportable"
    assert "don't tell the others" not in reply


def test_a_channel_claim_does_carry_the_words(wired: sqlite3.Connection) -> None:
    """The CONTROL for the DM rule: a suppression that hid everything would pass it."""
    _take(KERRY, said="I'm taking Gobles, already spoke to them", channel=CHANNEL)
    reply = _take(NELLY, said="mine")
    assert "already spoke to them" in reply


def test_someone_off_the_roster_cannot_claim(wired: sqlite3.Connection) -> None:
    """Fail closed: Grant later names the holder to a colleague, so it must know them."""
    reply = _take(STRANGER)
    assert reply.startswith("ERROR")
    assert _live(wired) == {}


def test_release_hands_it_back(wired: sqlite3.Connection) -> None:
    """And says how many rows moved, rather than a bare 'done'."""
    _take(KERRY)
    reply = _take(KERRY, said="I'm off Gobles", release=True)
    assert "back in the pool" in reply
    assert _live(wired) == {}


def test_releasing_something_nobody_holds_says_so(wired: sqlite3.Connection) -> None:
    """Never a soothing 'done' for work that did not happen."""
    reply = _take(KERRY, said="drop Gobles", release=True)
    assert "nothing" in reply.lower()


def test_search_marks_a_claimed_row_with_a_name_not_an_id(
    wired: sqlite3.Connection,
) -> None:
    """This is where a SECOND rep discovers a lead is taken."""
    _take(KERRY)
    rendered = claimed_phrases(wired, [1, 2, 4])
    assert rendered[1].startswith("Kerry (")
    assert KERRY not in rendered[1]
    assert 4 not in rendered, "an unclaimed lead carries no marker at all"


def test_search_names_the_date_when_the_roster_cannot_name_the_person(
    wired: sqlite3.Connection,
) -> None:
    """ "Somebody took it and I can't say who" is a different fact from "nobody did".

    A raw id would be meaningless in a spreadsheet a rep forwards on, so the date
    stands alone rather than the identifier.
    """
    wired.execute(
        """INSERT INTO lead_claims
             (lead_id,slack_user,audience,thread_ts,message_ts,claim_text,claimed_at)
           VALUES (4,?, ?,'1.0','1.0','mine','2026-09-01T19:22:39+00:00')""",
        (STRANGER, CHANNEL),
    )
    wired.commit()
    rendered = claimed_phrases(wired, [4])
    assert rendered[4] == "claimed 2026-09-01"
    assert STRANGER not in rendered[4]


def test_a_search_on_a_database_without_the_table_still_works(
    tmp_path: Path,
) -> None:
    """A pre-migration database loses the markers, not the search.

    `search_leads` opens `mode=ro` and therefore never migrates. Letting the missing
    table raise would turn every search into "ERROR: search failed" over a feature
    the question had nothing to do with.
    """
    bare = sqlite3.connect(tmp_path / "bare.db")
    bare.row_factory = sqlite3.Row
    assert claimed_phrases(bare, [1, 2, 3]) == {}


def test_a_raw_mention_already_in_the_ledger_is_still_defused_on_the_way_out(
    wired: sqlite3.Connection,
) -> None:
    """The guard on the READ path, proven without the write path masking it.

    `claim_lead` defuses before storing, so an end-to-end test cannot tell whether
    the quote path defuses too — it passes either way, which is how that assertion
    was silently vacuous. The ledger is a table, and anything that writes to it later
    (a backfill, a migration, another tool) is outside this module's control. So the
    property worth pinning is: whatever is IN the ledger, what comes OUT is inert.
    """
    wired.execute(
        """INSERT INTO lead_claims
             (lead_id,slack_user,audience,thread_ts,message_ts,claim_text,claimed_at)
           VALUES (1,?,?,'1.0','1.0',
                   '<!here> mine, ask <@U01DFJWQQJ3> <!subteam^S123>',
                   '2026-09-01T19:22:39+00:00')""",
        (KERRY, CHANNEL),
    )
    wired.commit()
    reply = _take(NELLY, said="I'll take Gobles")
    assert "<!" not in reply and "<@" not in reply
    assert "@here" in reply, "the words a reader saw are preserved, just made inert"
