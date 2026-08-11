"""Buying contacts in bulk must be bounded, targeted, and never pay twice.

This module spends real money, so the tests that matter are the ones about the
ceiling holding and the credit going to the right person.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from grant_watch import contact_fill, db


@dataclass(frozen=True)
class _Match:
    """A ZoomInfo search hit, with only the fields ranking reads."""

    person_id: str
    job_title: str
    has_email: bool = True
    contact_accuracy_score: float = 90.0


def test_a_decision_maker_outranks_a_lacrosse_coach() -> None:
    """ZoomInfo returns whoever it has; an unranked list buys the wrong person.

    A real search of a school district returned Head Custodian, Head Lacrosse Coach
    and Head Volleyball Coach alongside the Interim CTO. Each costs the same credit.
    """
    ranked = contact_fill.rank_candidates(
        [
            _Match("1", "Head Lacrosse Coach"),
            _Match("2", "Chief Technology Officer"),
            _Match("3", "Head Custodian"),
            _Match("4", "Chief Business Officer"),
        ]
    )
    assert [m.person_id for m in ranked][:2] == ["2", "4"]


def test_a_contactable_person_outranks_a_higher_accuracy_one_without_email() -> None:
    """A perfect record with no way to reach them is worth less than a reachable one."""
    ranked = contact_fill.rank_candidates(
        [
            _Match(
                "1",
                "Director of Technology",
                has_email=False,
                contact_accuracy_score=99,
            ),
            _Match(
                "2", "Director of Technology", has_email=True, contact_accuracy_score=70
            ),
        ]
    )
    assert [m.person_id for m in ranked] == ["2", "1"]


def test_a_superintendent_still_beats_nobody() -> None:
    """Small districts have no technology title at all; ranking must not empty out."""
    ranked = contact_fill.rank_candidates([_Match("1", "Superintendent")])
    assert [m.person_id for m in ranked] == ["1"]


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database, never the developer's own."""
    return db.connect(tmp_path / "fill.db")


def _lead(conn: sqlite3.Connection, name: str) -> int:
    """One lead with the columns the preview reads."""
    cur = conn.execute(
        """INSERT INTO leads
             (entity_name,state,source,source_item_id,detail_url,lead_grade)
           VALUES (?,?,?,?,?,?)""",
        (name, "CA", "test", name, f"https://example.gov/{name}", "gold"),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _patch_search(
    monkeypatch: pytest.MonkeyPatch, per_org: int, calls: list[int] | None = None
) -> None:
    """Make the FREE search return a fixed roster, recording each lead it saw."""
    from grant_watch.enrich import zoominfo_enrichment

    def fake_preview(_conn: object, lead_id: int, **_kw: object) -> object:
        """A preview built without touching the network."""
        if calls is not None:
            calls.append(lead_id)
        return zoominfo_enrichment.ZoomInfoPreview(
            lead_id=lead_id,
            entity_name="Test District",
            matches=tuple(
                _Match(f"{lead_id}-{i}", "Chief Technology Officer")
                for i in range(per_org)
            ),
            consumed=0,
            limit=1000,
        )

    monkeypatch.setattr(zoominfo_enrichment, "preview_for_lead", fake_preview)


def test_the_credit_ceiling_stops_the_run_before_money_moves(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is a LIMIT, not a report written after the spending.

    Five leads at two credits each is ten; a budget of five must buy two leads and
    decline the rest rather than discovering the overrun mid-purchase.
    """
    _patch_search(monkeypatch, per_org=3)
    ids = [_lead(conn, f"District {i}") for i in range(5)]
    out = contact_fill.fill_contacts(conn, ids, max_credits=5, dry_run=True)
    assert out.credits_spent <= 5
    assert out.filled == 2
    assert out.skipped_budget == 3
    conn.close()


def test_a_lead_that_already_has_a_contact_is_never_re_bought(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paying twice for the same person is the easiest money to waste."""
    searched: list[int] = []
    _patch_search(monkeypatch, per_org=2, calls=searched)
    lead_id = _lead(conn, "Already Known")
    conn.execute(
        """INSERT INTO contacts (lead_id,name,title,email,contact_status)
           VALUES (?,?,?,?,'vendor_licensed')""",
        (lead_id, "Vic Chalabian", "IT Manager", "v@example.org"),
    )
    conn.commit()
    out = contact_fill.fill_contacts(conn, [lead_id], max_credits=50, dry_run=True)
    assert out.skipped_have_contact == 1
    assert out.credits_spent == 0
    assert searched == [], "it searched a lead it had no intention of buying"
    conn.close()


def test_a_linkedin_only_row_does_not_count_as_having_a_contact(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """73% of production contacts carry no email, phone or mobile at all.

    Treating those as "has a contact" would skip exactly the leads this exists for.
    """
    _patch_search(monkeypatch, per_org=2)
    lead_id = _lead(conn, "Only LinkedIn")
    conn.execute(
        """INSERT INTO contacts (lead_id,name,title,contact_status)
           VALUES (?,?,?,'linkedin_only')""",
        (lead_id, "Someone", "Teacher"),
    )
    conn.commit()
    out = contact_fill.fill_contacts(conn, [lead_id], max_credits=50, dry_run=True)
    assert out.skipped_have_contact == 0
    assert out.filled == 1
    conn.close()


def test_a_dry_run_spends_nothing_and_still_reports_the_bill(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator must be able to see the exact cost before authorising it."""
    _patch_search(monkeypatch, per_org=4)
    ids = [_lead(conn, f"D{i}") for i in range(3)]
    out = contact_fill.fill_contacts(conn, ids, max_credits=100, dry_run=True)
    assert out.credits_spent == 6  # 3 leads x PER_LEAD
    assert contact_fill.zoominfo_credits.usage(conn)[0] == 0, "a dry run billed"
    conn.close()


def test_no_matches_is_not_an_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Small rural districts are simply absent from ZoomInfo."""
    _patch_search(monkeypatch, per_org=0)
    out = contact_fill.fill_contacts(
        conn, [_lead(conn, "Tiny School")], max_credits=10, dry_run=True
    )
    assert out.skipped_no_match == 1
    assert out.credits_spent == 0
    conn.close()


def test_a_zero_budget_buys_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degenerate case must decline rather than divide by zero or buy one anyway."""
    _patch_search(monkeypatch, per_org=2)
    out = contact_fill.fill_contacts(
        conn, [_lead(conn, "D")], max_credits=0, dry_run=True
    )
    assert out.credits_spent == 0
    assert out.filled == 0
    conn.close()


def test_a_lead_id_from_another_org_is_skipped_not_patched() -> None:
    """`salesforce_id` holds BOTH production and sandbox ids, with nothing marking which.

    A bulk fill therefore meets ids that do not exist in this org. The pre-read is
    what makes that safe — nothing is patched — but it used to be reported as a raw
    `HTTP 404`, which reads like a broken integration rather than a routine skip.
    """
    import requests

    from grant_watch.enrich import salesforce_campaign_gateway as gw

    calls: list[str] = []

    class _Resp:
        """A Salesforce response with a chosen status."""

        def __init__(self, code: int) -> None:
            """Hold the status code."""
            self.status_code = code
            self.text = "not found"

        def json(self) -> dict[str, object]:
            """Never reached for a 404."""
            return {}

    def fake_get(url: str, **_kw: object) -> _Resp:
        """The pre-read, answering as the org would for a foreign id."""
        calls.append("GET")
        return _Resp(404)

    def fake_patch(url: str, **_kw: object) -> _Resp:
        """Must never be reached."""
        calls.append("PATCH")
        return _Resp(204)

    with pytest.MonkeyPatch.context() as mp:
        for var in (
            "SALESFORCE_WRITE_CLIENT_ID",
            "SALESFORCE_WRITE_CLIENT_SECRET",
            "SALESFORCE_CLIENT_ID",
            "SALESFORCE_CLIENT_SECRET",
        ):
            mp.setenv(var, "x")
        mp.setenv("SALESFORCE_MY_DOMAIN_URL", "https://example.my.salesforce.com")
        mp.setenv("SALESFORCE_WRITE_ORG_ID", "00D000000000000EAM")
        mp.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")
        gateway = gw.SalesforceCampaignGateway()
        mp.setattr(
            gw.SalesforceCampaignGateway,
            "_auth",
            lambda self, force=False: ("tok", "https://example.my.salesforce.com"),
        )
        mp.setattr(
            gw.SalesforceCampaignGateway, "verify_write_scope", lambda self: None
        )
        mp.setattr(requests, "get", fake_get)
        mp.setattr(requests, "patch", fake_patch)
        result = gateway.fill_lead_blanks("00QVC00000abcde2AA", {"Title": "CTO"})

    assert calls == ["GET"], "a record that does not exist here was patched"
    assert result.error == "not in this org"
    assert result.success is False


def _row(**fields: object) -> dict[str, object]:
    """A contact row with the columns the evidence sentence reads."""
    base = {
        "source_url": "",
        "contact_status": "vendor_licensed",
        "provenance": "vendor_licensed",
        "do_not_call": 0,
        "asserted_by_slack_user": "",
        "asserted_at": "",
    }
    base.update(fields)
    return base


def test_do_not_call_is_stated_in_the_salesforce_record_and_stated_first() -> None:
    """The flag has to travel with the person, not stop at our own database.

    Blanking the number locally is airtight while it stays inside Grant, and worth
    nothing once a rep gets it another way: the CRM record names a real human with no
    marker, and an empty Phone reads as "we don't have it" rather than "do not call".
    It leads the sentence because a compliance fact a rep has to scroll for is one
    they will miss.
    """
    from grant_watch.enrich import salesforce_contact_records as scr

    flagged = scr._contact_evidence(_row(do_not_call=1))
    assert flagged.startswith("DO NOT CALL"), flagged
    assert "must not be dialled" in flagged
    # The provenance claim is still made — the warning adds to it, never replaces it.
    assert "ZoomInfo" in flagged

    clean = scr._contact_evidence(_row(do_not_call=0))
    assert "DO NOT CALL" not in clean, "a callable contact was labelled do-not-call"
    assert "ZoomInfo" in clean


def test_do_not_call_also_marks_a_linkedin_sourced_person() -> None:
    """Every evidence class needs it, not just the vendor one.

    Fixing only the path in front of you is how the opt-out shipped for the legacy
    drip while the rich card — the thing that actually posts — ignored it.
    """
    from grant_watch.enrich import salesforce_contact_records as scr

    text = scr._contact_evidence(
        _row(
            contact_status="linkedin_only", provenance="linkedin_claimed", do_not_call=1
        )
    )
    assert text.startswith("DO NOT CALL")
    assert "LinkedIn" in text


class _Sf:
    """A Salesforce stand-in recording the exact call sequence and payload."""

    def __init__(self, description: str, get_code: int = 200) -> None:
        """Seed the stored Description and the read's status."""
        self.description = description
        self.get_code = get_code
        self.calls: list[str] = []
        self.patched: dict[str, object] | None = None

    def get(self, url: str, **_kw: object) -> object:
        """The mandatory pre-read."""
        self.calls.append("GET")
        outer = self

        class _R:
            status_code = outer.get_code
            text = "err"

            def json(self) -> dict[str, object]:
                """The stored record."""
                return {"Description": outer.description}

        return _R()

    def patch(self, url: str, **kw: object) -> object:
        """The append."""
        self.calls.append("PATCH")
        self.patched = dict(kw.get("json") or {})

        class _R:
            status_code = 204
            text = ""

        return _R()


def _gateway(mp: pytest.MonkeyPatch) -> object:
    """A gateway with auth and the write gate stubbed out."""
    from grant_watch.enrich import salesforce_campaign_gateway as gw

    for var in (
        "SALESFORCE_WRITE_CLIENT_ID",
        "SALESFORCE_WRITE_CLIENT_SECRET",
        "SALESFORCE_CLIENT_ID",
        "SALESFORCE_CLIENT_SECRET",
    ):
        mp.setenv(var, "x")
    mp.setenv("SALESFORCE_MY_DOMAIN_URL", "https://example.my.salesforce.com")
    mp.setenv("SALESFORCE_WRITE_ORG_ID", "00D000000000000EAM")
    mp.setenv("SALESFORCE_CAMPAIGN_WRITES_ENABLED", "1")
    mp.setattr(
        gw.SalesforceCampaignGateway,
        "_auth",
        lambda self, force=False: ("t", "https://example.my.salesforce.com"),
    )
    mp.setattr(gw.SalesforceCampaignGateway, "verify_write_scope", lambda self: None)
    return gw.SalesforceCampaignGateway()


def test_marking_do_not_call_prepends_and_keeps_every_existing_character() -> None:
    """The one write that touches a non-empty field must be unable to lose anything."""
    import requests

    from grant_watch.enrich import salesforce_campaign_gateway as gw

    original = "Created by Grant as an organization-only lead. Grant lead 231."
    fake = _Sf(original)
    with pytest.MonkeyPatch.context() as mp:
        gateway = _gateway(mp)
        mp.setattr(requests, "get", fake.get)
        mp.setattr(requests, "patch", fake.patch)
        result = gateway.mark_lead_do_not_call("00QUZ00000byrvN2AQ")

    assert result.success is True
    written = str((fake.patched or {}).get("Description"))
    assert written.startswith(gw.DO_NOT_CALL_MARKER), "the warning must lead"
    assert written.endswith(original), "existing text was altered or truncated"
    assert original in written


def test_marking_twice_does_not_repeat_the_warning() -> None:
    """A compliance sentence printed four times teaches people to ignore it."""
    import requests

    from grant_watch.enrich import salesforce_campaign_gateway as gw

    fake = _Sf(f"{gw.DO_NOT_CALL_MARKER} Created by Grant.")
    with pytest.MonkeyPatch.context() as mp:
        gateway = _gateway(mp)
        mp.setattr(requests, "get", fake.get)
        mp.setattr(requests, "patch", fake.patch)
        result = gateway.mark_lead_do_not_call("00QUZ00000byrvN2AQ")

    assert result.error == "already marked"
    assert fake.calls == ["GET"], "an already-marked record was written again"


def test_marking_a_record_from_another_org_is_a_skip() -> None:
    """Same foreign-id case as the fill: read first, never patch."""
    import requests

    fake = _Sf("", get_code=404)
    with pytest.MonkeyPatch.context() as mp:
        gateway = _gateway(mp)
        mp.setattr(requests, "get", fake.get)
        mp.setattr(requests, "patch", fake.patch)
        result = gateway.mark_lead_do_not_call("00QVC00000abcde2AA")

    assert result.error == "not in this org"
    assert fake.calls == ["GET"]


def test_a_foreign_org_skip_does_not_make_fill_leads_exit_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run where every real record was written perfectly must exit 0.

    The 8 ids belonging to the monarchdev sandbox were counted as errors, so a
    flawless run reported failure. Harmless while a human reads the output, and
    exactly the kind of thing that reads as a broken job once it goes in cron.
    """
    from grant_watch import db, salesforce_lead_fill
    from grant_watch.enrich.salesforce_campaign_gateway import CreateResult

    conn = db.connect(tmp_path / "x.db")
    monkeypatch.setattr(
        salesforce_lead_fill,
        "linked_leads",
        lambda _c, _l: [{"lead_id": 1, "salesforce_id": "00QVC00000abcde2AA"}],
    )
    monkeypatch.setattr(
        salesforce_lead_fill, "proposed_fields", lambda _c, _l: {"Title": "CTO"}
    )

    class _Client:
        """A gateway that only ever meets a foreign-org record."""

        def fill_lead_blanks(self, record_id: str, fields: dict) -> CreateResult:
            """Report the routine skip."""
            return CreateResult(False, record_id, error="not in this org")

    outcome = salesforce_lead_fill.run(conn, _Client(), limit=10, dry_run=False)
    assert outcome.failed == 0, "a routine skip was counted as an error"
    conn.close()


def test_an_email_carries_the_whole_list_and_a_chat_message_does_not() -> None:
    """Two caps, one destination question.

    Kerry asked twice to be SENT the list and received "15 of 81". Fifteen rows is
    right in Slack, where a longer list buries the channel and nobody scrolls it, and
    wrong in an inbox, where having the list is the entire reason she asked.
    """
    from grant_watch.slack import search

    assert search.EMAIL_ROW_CAP > 15
    src = Path(search.__file__).read_text()
    assert "display_cap = 15 if for_chat else EMAIL_ROW_CAP" in src, (
        "the row cap stopped depending on the destination"
    )


def test_the_trailer_does_not_tell_an_email_reader_to_refine_their_search() -> None:
    """The same leak as the spreadsheet offer, one string further down.

    "refine the search or export all results" is an instruction nobody can act on
    from an inbox — and it arrived in the one thread where being handed an action
    instead of an answer is the whole complaint.
    """
    from grant_watch.slack import search

    src = Path(search.__file__).read_text()
    trailer_block = src[src.index("if total > shown:") : src.index("more = trailer")]
    assert "if for_chat" in trailer_block, "the trailer ignores its destination"
    assert "Ask me in Slack for the rest" in trailer_block


def test_a_repeat_pull_skips_that_lead_instead_of_aborting_a_paid_batch(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One poisoned lead must not discard the outcome of leads already bought.

    Reachable and compounding: a lead whose chosen people all return NO_MATCH bills
    nothing and stores nothing, so it is re-selected next run, the ranking is
    deterministic so the same two people are chosen, the request key is identical,
    and the ledger refuses. Before this, that refusal escaped the loop — every
    earlier lead in the batch was billed and stored, and the rep saw only a failure.
    """
    from grant_watch.enrich import zoominfo_enrichment
    from grant_watch.enrich.zoominfo_credits import AlreadySpent

    _patch_search(monkeypatch, per_org=2)
    ids = [_lead(conn, f"D{i}") for i in range(3)]
    calls: list[int] = []

    def flaky(_conn: object, lead_id: int, _people: list, **_kw: object) -> object:
        """The middle lead was already paid for."""
        calls.append(lead_id)
        if lead_id == ids[1]:
            raise AlreadySpent("this ZoomInfo pull already completed")
        return zoominfo_enrichment.ZoomInfoApplied(
            stored=2, billed=2, suppressed_numbers=0
        )

    monkeypatch.setattr(zoominfo_enrichment, "apply_for_lead", flaky)
    out = contact_fill.fill_contacts(conn, ids, max_credits=40, dry_run=False)

    assert calls == ids, "the run stopped instead of continuing past the repeat"
    assert out.filled == 2
    assert out.credits_spent == 4
    conn.close()


def test_the_bulk_tool_refuses_a_ceiling_the_model_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model supplies max_credits, so it cannot be the only ceiling.

    The two-step "price it, then confirm" protocol lives in the tool DESCRIPTION,
    which is a prompt instruction — and a model may call confirm=true on its first
    turn. Several tool_use blocks across six turns compound it. The cap has to be in
    the code.
    """
    from grant_watch.slack import tools

    spent: list[int] = []
    monkeypatch.setattr(
        tools.contact_fill if hasattr(tools, "contact_fill") else tools,
        "__name__",
        "tools",
        raising=False,
    )
    out = tools._zoominfo_fill_many([1, 2], 997, True, "U0REP")
    assert out.startswith("ERROR:")
    assert str(tools.MAX_CREDITS_PER_CALL) in out
    assert spent == []


def test_only_one_paid_bulk_pull_may_run_per_human_message() -> None:
    """The per-CALL cap does not bound how many calls a turn makes.

    `MAX_CREDITS_PER_CALL = 40` limits one call. The agent loop runs six tool turns
    with several blocks each, and the result cache is keyed on exact arguments — so
    varying `lead_ids` defeats it. One rep saying "fill in all the gold leads" could
    otherwise spend the month with nobody seeing a price.

    The same rule already existed for `email_results`, with reasoning that applies
    more forcefully here: money is less recoverable than an email.
    """
    from grant_watch.slack.conversation import _single_execution_tool_key

    paid = _single_execution_tool_key("zoominfo_fill_many", {"confirm": True})
    assert paid, "an unbounded number of paid bulk pulls may run in one turn"
    # Keyed WITHOUT the arguments, so varying the lead set cannot buy a second run.
    other = _single_execution_tool_key(
        "zoominfo_fill_many", {"confirm": True, "lead_ids": [9, 9, 9]}
    )
    assert other == paid, "changing the lead ids bought another paid pull"

    # Pricing is free and must stay repeatable.
    assert _single_execution_tool_key("zoominfo_fill_many", {"confirm": False}) == ""

    # The per-lead paid tool needs the same bound.
    assert _single_execution_tool_key("zoominfo_enrich_contacts", {"lead_id": 1})


def test_an_emailed_row_carries_a_clickable_link_not_slack_markup() -> None:
    """`<url|label>` is Slack mrkdwn, and `send_to_rep` posts a text-only payload.

    So every emailed row arrived carrying literal angle brackets and a pipe. The one
    element on that line that must survive intact is the honesty link — the thing a
    rep clicks to check Grant is not making the award up — and it was the element
    being mangled.
    """
    import re

    from grant_watch import lead_digest
    from grant_watch.slack.search import search_leads

    email_body = lead_digest.render({"program": "SVPP", "limit": 3})
    assert email_body, "no leads rendered; the assertion below would be vacuous"
    assert not re.search(r"<https?://[^>]*\|", email_body), (
        "raw Slack mrkdwn reached an inbox"
    )
    assert "verify this record" in email_body, "the honesty link vanished entirely"

    chat_body, _ = search_leads(program="SVPP", limit=3)
    assert re.search(r"<https?://[^>]*\|", chat_body), "Slack lost its clickable links"


def test_confirm_without_a_priced_run_spends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`confirm=true` on the first call must not reach the provider.

    The two-step protocol lived only in the tool DESCRIPTION — a prompt
    instruction — while this module's own comment said the safety must be the
    shape. Raising the ceiling from 40 to 100 credits made that gap worth 100
    credits of un-approved spend from a single model turn, so the protocol is now
    enforced here instead of described.
    """
    from grant_watch.slack import tools

    tools._PRICED_RUNS.clear()
    spent: list[object] = []
    monkeypatch.setattr(
        contact_fill,
        "fill_contacts",
        lambda *a, **k: spent.append(k) or _never_called(),
    )
    out = tools._zoominfo_fill_many([1, 2], 10, True, "U0REP")
    assert out.startswith("ERROR:")
    assert "priced" in out
    assert spent == []


def test_pricing_first_then_confirming_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control: the honest sequence still works, and only for that lead set."""
    from grant_watch.slack import tools

    tools._PRICED_RUNS.clear()
    calls: list[bool] = []

    class _Outcome:
        """Minimal stand-in for a fill outcome."""

        def summary(self) -> str:
            """Describe the run."""
            return "considered 2, filled 2"

    def fake_fill(_conn: object, _ids: object, **kwargs: object) -> _Outcome:
        """Record whether this was a priced run or a real one."""
        calls.append(bool(kwargs["dry_run"]))
        return _Outcome()

    monkeypatch.setattr(contact_fill, "fill_contacts", fake_fill)
    monkeypatch.setattr(contact_fill, "remaining_credits", lambda _c: 900)
    monkeypatch.setattr(db, "connect", lambda *a, **k: sqlite3.connect(":memory:"))
    assert tools._zoominfo_fill_many([1, 2], 10, False, "U0REP").startswith("PRICED")
    assert tools._zoominfo_fill_many([1, 2], 10, True, "U0REP").startswith("BOUGHT")
    assert calls == [True, False]
    # A DIFFERENT lead set is not covered by that pricing.
    assert tools._zoominfo_fill_many([3, 4], 10, True, "U0REP").startswith("ERROR:")
    # Nor is a different rep.
    assert tools._zoominfo_fill_many([1, 2], 10, True, "U0OTHER").startswith("ERROR:")


def _never_called() -> object:
    """Fail loudly if the provider path is reached."""
    raise AssertionError("the paid path must not be reached")
