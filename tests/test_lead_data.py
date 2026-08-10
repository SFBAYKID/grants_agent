"""What Grant fills into a lead, and what it is allowed to CLAIM about it.

Split from test_reminders.py at the 1,000-line cap (CLAUDE.md rule 4). The boundary
is real: these are about LEAD DATA and the durable claims made from it — the
organization sweep that populates addresses, and the evidence sentence written into a
Salesforce record — while the file they left is about follow-ups and email.

The sentence tests matter most. That string outlives every Slack thread, so it is the
most durable claim Grant makes about a person.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from grant_watch import db

CHANNEL = "C0TEST"
REP = "U0REP"
THREAD = "1700000000.000100"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A throwaway migrated database."""
    return db.connect(tmp_path / "leads.db")


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


# --- What Salesforce is told about a contact's evidence ----------------------------


@pytest.mark.parametrize(
    ("writer", "kwargs", "must_say", "must_not_say"),
    [
        (
            "save_vendor_contact",
            {
                "name": "Vic Chalabian",
                "title": "IT Manager",
                "email": "v@x.test",
                "phone": "",
                "vendor_person_id": "1",
                "do_not_call": False,
            },
            "Supplied by ZoomInfo",
            "verified verbatim",
        ),
        (
            "save_linkedin_contact",
            {
                "name": "Dana Reyes",
                "title": "Director",
                "profile_url": "https://linkedin.test/in/dr",
            },
            "ownership not verified",
            "verified verbatim",
        ),
    ],
)
def test_salesforce_is_never_told_a_contact_was_verified_when_it_was_not(
    tmp_path: Path,
    writer: str,
    kwargs: dict[str, object],
    must_say: str,
    must_not_say: str,
) -> None:
    """This sentence is written into a CRM record and outlives every thread.

    It special-cased only LinkedIn and fell through to "Contact verified verbatim on
    {source}" for everything else — so a ZoomInfo contact, which has no source URL,
    was filed as "Contact verified verbatim on unknown source": a claim of
    verification, citing nothing, about data nobody checked.
    """
    from grant_watch.enrich.salesforce_contact_records import _contact_evidence

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('s','1','Test District','CA','u')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    getattr(db, writer)(conn, lead_id, **kwargs)
    contact = conn.execute(
        "SELECT * FROM contacts WHERE lead_id=?", (lead_id,)
    ).fetchone()

    sentence = _contact_evidence(contact)
    assert must_say in sentence, sentence
    assert must_not_say not in sentence, (
        f"claimed verification it does not have: {sentence}"
    )
    conn.close()


def test_a_contact_with_no_source_page_does_not_claim_verification(
    tmp_path: Path,
) -> None:
    """ "Contact verified verbatim on unknown source" was a real string it produced."""
    from grant_watch.enrich.salesforce_contact_records import _contact_evidence

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url) "
        "VALUES ('s','1','Test District','CA','u')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_contact(conn, lead_id, "Dana", "Director", "d@x.test", "", "", "medium")
    contact = conn.execute(
        "SELECT * FROM contacts WHERE lead_id=?", (lead_id,)
    ).fetchone()

    sentence = _contact_evidence(contact)
    assert "verified verbatim" not in sentence
    assert "unknown source" not in sentence
    conn.close()


def test_the_sweep_pays_for_each_organization_once(tmp_path: Path) -> None:
    """The first production run bought the same page three times.

    Gold holds ~30 duplicated entity names, and the sweep pays per LEAD row, so
    Modesto City Schools was scraped twice and Mt. Morris three times in a single
    25-lead batch. Each organization should cost one fetch.
    """
    from grant_watch import org_backfill

    conn = _conn(tmp_path)
    for index, name in enumerate(
        [
            "MODESTO CITY SCHOOLS",
            "MODESTO CITY SCHOOLS",
            "MT MORRIS",
            "MT MORRIS",
            "MT MORRIS",
            "GALT JOINT UNION",
        ]
    ):
        conn.execute(
            "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
            "lead_grade,canonical_entity_key,amount) VALUES ('s',?,?,'CA','u','gold',?,?)",
            (str(index), name, name.lower().replace(" ", "") + "|ca", 100),
        )
    conn.commit()
    picked = org_backfill.candidates(conn, grade="gold")
    names = [row["entity_name"] for row in picked]
    assert len(names) == len(set(names)) == 3, f"paid for a duplicate: {names}"
    conn.close()


def _linked(conn: sqlite3.Connection, lead_id: int, sf_id: str, item: int) -> None:
    """Record that a Grant lead corresponds to a Salesforce Lead.

    The parent `crm_actions` row is required: `crm_action_items.action_id` carries a
    foreign key, and inserting items without it silently tested nothing.
    """
    conn.execute(
        "INSERT OR IGNORE INTO crm_actions (id,action_type,workspace,channel,"
        "thread_ts,requested_by,state,payload_json,payload_hash,nonce_hash,"
        "expires_at,attempts,external_write_started,created_at,updated_at) "
        "VALUES (?,'add_campaign_members','T1','C1','1.1','U0REP','complete','{}',"
        "'h','n','2026-08-02',0,0,'2026-08-01','2026-08-01')",
        (f"act-{item}",),
    )
    conn.execute(
        "INSERT INTO crm_action_items (id,action_id,lead_id,canonical_entity_key,"
        "operation,proposed_json,state,verification_state,salesforce_id) "
        "VALUES (?,?,?,'k','add_member','{}','complete','verified',?)",
        (item, f"act-{item}", lead_id, sf_id),
    )


def test_a_linkedin_title_never_becomes_a_salesforce_field(tmp_path: Path) -> None:
    """`linkedin_only` means ownership is UNPROVEN, and a Salesforce Title says nothing.

    Writing an unverified title into a structured CRM field launders a guess into a
    fact that outlives every thread, and nobody downstream can tell it from a checked
    one. Grant still surfaces LinkedIn findings in Slack, where they are labelled.
    """
    from grant_watch import salesforce_lead_fill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade) VALUES ('s','1','Test District','CA','u','gold')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_linkedin_contact(
        conn, lead_id, "Dana Reyes", "Director of Technology", "https://li.test/in/dr"
    )
    conn.commit()

    offered = salesforce_lead_fill.proposed_fields(conn, lead_id)
    assert "Title" not in offered, (
        f"an unverified LinkedIn title was offered to Salesforce: {offered}"
    )
    conn.close()


def test_a_verified_title_is_offered(tmp_path: Path) -> None:
    """The other direction: a page-verified contact SHOULD complete the record."""
    from grant_watch import salesforce_lead_fill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade) VALUES ('s','1','Test District','CA','u','gold')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    db.save_contact(
        conn,
        lead_id,
        "Dana Reyes",
        "Director of Technology",
        "d@x.test",
        "555-0100",
        "https://district.test/staff",
        "high",
    )
    conn.commit()

    offered = salesforce_lead_fill.proposed_fields(conn, lead_id)
    assert offered.get("Title") == "Director of Technology"
    assert offered.get("Email") == "d@x.test"
    conn.close()


def test_one_grant_lead_yields_one_salesforce_target(tmp_path: Path) -> None:
    """Lead #231 maps to TWO Salesforce records and appeared twice.

    The same values would have gone to both, and --limit would have bounded rows
    rather than leads. Merging two CRM records for one organization is a human's
    call, not something a sweep should do by writing to both.
    """
    from grant_watch import salesforce_lead_fill

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade) VALUES ('s','1','Birmingham Community Charter','CA','u','gold')"
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT id FROM leads").fetchone()["id"])
    _linked(conn, lead_id, "00Q000000000001", 1)
    _linked(conn, lead_id, "00Q000000000002", 2)
    conn.commit()

    rows = salesforce_lead_fill.linked_leads(conn)
    assert len(rows) == 1, f"one lead produced {len(rows)} write targets"
    assert rows[0]["salesforce_id"] == "00Q000000000001"
    conn.close()


def test_a_failed_org_lookup_never_reaches_salesforce(tmp_path: Path) -> None:
    """MEASURED ON PRODUCTION: two leads carried org_website='https://cde.ca.gov'.

    That is the California Department of Education, not the district — and a third
    carried a CMS vendor's CDN. A failed lookup still leaves `org_website` holding
    whatever URL the search landed on, and `organization_fields` read it without
    consulting `org_profile_status`.

    It matters more here than for most bad writes because the fill path only ever
    writes into EMPTY fields: once cde.ca.gov lands in Website, that field is closed
    to the tool forever and a later run after a fix skips it. The tool that makes the
    error can never correct it.
    """
    from grant_watch.enrich.salesforce_contact_records import organization_fields

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade,org_profile_status,org_website,org_street,org_postal_code,"
        "org_city) VALUES ('s','1','Valle Lindo School District','CA','u','gold',"
        "'not_found','https://cde.ca.gov','1 Wrong St','00000','Sacramento')"
    )
    conn.commit()
    lead = db.get_lead(conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"]))

    offered = organization_fields(lead)
    for field in ("Website", "Street", "PostalCode"):
        assert field not in offered, (
            f"a not_found org profile leaked {field}={offered[field]!r} to Salesforce"
        )
    # The lead's OWN facts are still safe to offer — they do not come from the lookup.
    assert offered.get("State") == "CA"
    conn.close()


def test_a_found_org_profile_is_still_offered(tmp_path: Path) -> None:
    """The other direction: gating must not silence a lookup that actually worked."""
    from grant_watch.enrich.salesforce_contact_records import organization_fields

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade,org_profile_status,org_website,org_street,org_postal_code,"
        "org_city) VALUES ('s','1','Montebello Unified','CA','u','gold','found',"
        "'https://montebello.k12.ca.us','123 Main St','90640','Montebello')"
    )
    conn.commit()
    lead = db.get_lead(conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"]))

    offered = organization_fields(lead)
    assert offered["Website"] == "https://montebello.k12.ca.us"
    assert offered["Street"] == "123 Main St"
    assert offered["PostalCode"] == "90640"
    assert offered["City"] == "Montebello"
    conn.close()


def test_the_org_phone_fallback_also_respects_a_failed_lookup(tmp_path: Path) -> None:
    """Same defect, a different surface — found while verifying the first fix.

    `choose_phone` falls back to the organization's main line when a person has no
    direct number, and read `org_phone` without consulting `org_profile_status`. A
    failed lookup leaves that column holding whatever the search landed on, exactly
    as `org_website` held `cde.ca.gov`. This feeds the contact-record payloads rather
    than `fill-leads`, so the first fix did not cover it.
    """
    from grant_watch.enrich.salesforce_contact_records import choose_phone

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO leads (source,source_item_id,entity_name,state,detail_url,"
        "lead_grade,org_profile_status,org_phone) VALUES ('s','1','Valle Lindo',"
        "'CA','u','gold','not_found','(916) 319-0800')"
    )
    conn.commit()
    lead = db.get_lead(conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"]))
    lead_id = int(lead["id"])
    db.save_linkedin_contact(conn, lead_id, "Dana", "Director", "https://li.test/in/d")
    contact = conn.execute(
        "SELECT * FROM contacts WHERE lead_id=?", (lead_id,)
    ).fetchone()

    number, kind = choose_phone(contact, lead)
    assert (number, kind) == ("", ""), (
        f"a failed org lookup supplied a phone number: {number!r}"
    )

    conn.execute("UPDATE leads SET org_profile_status='found' WHERE id=?", (lead_id,))
    conn.commit()
    lead = db.get_lead(conn, lead_id)
    assert choose_phone(contact, lead) == ("(916) 319-0800", "org_general")
    conn.close()


# --- The morning update -----------------------------------------------------------


def _announcement(conn: sqlite3.Connection, slug: str = "u1") -> None:
    """Record one authored update."""
    from grant_watch import announce

    path = Path(tempfile.mkdtemp()) / "a.json"
    path.write_text(
        json.dumps(
            {
                "announcements": [
                    {
                        "slug": slug,
                        "audience": CHANNEL,
                        "body": "Morning all — here's what changed.",
                        "capabilities": ["email_results"],
                    }
                ]
            }
        )
    )
    announce.load(conn, path)


def test_an_update_is_posted_once_and_only_once(tmp_path: Path) -> None:
    """A repeated "here's what's new" teaches a channel to ignore Grant.

    `posted_at` is written BEFORE the Slack call, so a crash between the two loses
    an update rather than duplicating one — the same reserve-before-send ordering as
    every other proactive sender here.
    """
    from grant_watch import announce

    posts: list[str] = []

    class _Client:
        """Records what would reach Slack."""

        def chat_postMessage(self, **kwargs: object) -> dict[str, object]:
            """Capture one post."""
            posts.append(str(kwargs.get("text", "")))
            return {"ok": True, "ts": "9.1"}

    conn = _conn(tmp_path)
    _announcement(conn)

    assert "announced" in announce.run(_Client(), conn, dry_run=False)
    assert len(posts) == 1
    assert "here's what changed" in posts[0]

    assert announce.run(_Client(), conn, dry_run=False) == "skip: nothing to announce"
    assert len(posts) == 1, "the same update was posted twice"
    conn.close()


def test_seeding_an_update_never_posts_it(tmp_path: Path) -> None:
    """Recording and announcing are separate acts, so a seed cannot message anyone."""
    from grant_watch import announce

    conn = _conn(tmp_path)
    _announcement(conn)
    item = announce.pending(conn)
    assert item is not None and item.slug == "u1"
    assert announce.run(None, conn, dry_run=True).startswith("[dry-run]")
    assert announce.pending(conn) is not None, "a dry run consumed the update"
    conn.close()


def test_the_same_update_cannot_be_loaded_twice(tmp_path: Path) -> None:
    """Re-running a seed must not queue a second copy of the same post."""

    conn = _conn(tmp_path)
    _announcement(conn)
    _announcement(conn)
    count = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    assert count == 1
    conn.close()


def test_rep_email_is_not_redirected_by_the_prospect_test_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One switch for two different risks is how a true feature became a false claim.

    `OUTREACH_TEST_EMAIL` exists to stop PROSPECT outreach reaching a school
    administrator. It was also redirecting reps' own results, so on production mail
    for Kerry, Nelly and Jocelyn went to the test mailbox while `email_results` told
    them "Sent it to …". Clearing it to fix that would have un-protected Persequor at
    the same time.
    """
    from grant_watch.notify import resend_client

    monkeypatch.setenv("OUTREACH_TEST_EMAIL", "someone-else@example.test")
    monkeypatch.delenv("RESEND_TEST_EMAIL", raising=False)
    assert resend_client.recipient_for("U01DPJVURHU") == "chase@monarchconnected.com"

    # Its own switch still works, for when a rep email genuinely needs redirecting.
    monkeypatch.setenv("RESEND_TEST_EMAIL", "inbox@example.test")
    assert resend_client.recipient_for("U01DPJVURHU") == "inbox@example.test"


def test_reseeding_updates_the_apology_but_never_the_quote(tmp_path: Path) -> None:
    """`record()` skips duplicates, so an edited correction never reached the DB.

    Right for the ASK — what a colleague said is verbatim forever — and wrong for the
    CORRECTION, which is Grant's own apology and gets reviewed and shortened. The
    consequence was measured: Kerry's Monday message was still 236 characters, over
    the limit, because the shortened wording lived only in the file.
    """
    from grant_watch import capability_asks

    conn = _conn(tmp_path)
    common = {
        "slack_user": REP,
        "audience": CHANNEL,
        "thread_ts": THREAD,
        "message_ts": "1784820389.857359",
        "capability": "email_results",
        "asked_at": "2026-07-23T15:26:29+00:00",
        "recorded_by": "test",
    }
    capability_asks.record(
        conn,
        ask_text="Email those to kerry@…",
        correction="A long-winded apology.",
        **common,
    )
    capability_asks.record(
        conn, ask_text="SOMETHING ELSE ENTIRELY", correction="Short one.", **common
    )

    row = conn.execute("SELECT ask_text, correction FROM capability_asks").fetchone()
    assert row["correction"] == "Short one.", "the edited apology never landed"
    assert row["ask_text"] == "Email those to kerry@…", (
        "a colleague's own words were rewritten"
    )
    conn.close()


def test_the_real_seeded_follow_ups_all_fit(tmp_path: Path) -> None:
    """Against the ACTUAL shipped data, not a convenient fixture.

    The length invariant was already tested — with a short correction written inside
    the test. Production carried a longer one, so Kerry's real Monday message was 236
    characters, over the limit, and every test was green. A test that supplies its own
    happy input measures the test, not the product.
    """
    from datetime import datetime as _dt, timezone as _tz

    from grant_watch import capability_asks
    from grant_watch.slack import nudges

    conn = _conn(tmp_path)
    shipped = json.loads(
        Path("data/capability_asks/unmet_asks_20260809.json").read_text()
    )
    for ask in shipped["asks"]:
        capability_asks.record(
            conn,
            slack_user=ask["slack_user"],
            audience=ask["audience"],
            thread_ts=ask["thread_ts"],
            message_ts=ask["message_ts"],
            ask_text=ask["ask_text"],
            capability=ask["capability"],
            asked_at=ask["asked_at"],
            recorded_by="test",
            correction=ask.get("correction", ""),
        )
    for capability in {ask["capability"] for ask in shipped["asks"]}:
        capability_asks.mark_available(conn, capability)

    found = [
        c
        for c in nudges.candidates(conn, _dt.now(_tz.utc))
        if c.subject_kind == "capability_now_available"
    ]
    assert len(found) == len(shipped["asks"]), "not every seeded ask became reachable"
    for candidate in found:
        for variant in ("a", "b"):
            text = nudges.build_message(candidate, variant)
            assert len(text) <= 220, (
                f"{candidate.target_slack} ({variant}) is {len(text)} chars: {text}"
            )
            assert "?" in text
            assert candidate.target_slack in text, "the wrong person is mentioned"
    conn.close()


# --- Prompt caching ---------------------------------------------------------------


def test_the_fixed_prefix_is_marked_for_caching() -> None:
    """~11,000 identical tokens were re-sent and re-billed on every single call.

    The system prompt (~6,200 tokens) and tool schemas (~5,000) are byte-identical
    every time, on every message from every rep, and on every turn of the tool loop.
    A cache breakpoint on each means the rest of the window reads them instead of
    reprocessing them.

    The marker caches everything UP TO it, so ONE marker on the final tool covers the
    whole array — marking each tool individually would waste breakpoints.
    """
    from grant_watch.slack import conversation

    system = conversation._cached_system()
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == conversation._SYSTEM

    schemas = conversation._cached_tools()
    marked = [s for s in schemas if "cache_control" in s]
    assert len(marked) == 1, "a breakpoint per tool wastes the budget"
    assert marked[0] is schemas[-1], "the marker must sit on the LAST tool"


def test_caching_never_mutates_the_shared_schema_list() -> None:
    """`tools.TOOL_SCHEMAS` is a module-level list shared with every other caller.

    Marking it in place would let an unrelated import order decide whether a
    cache_control key exists on the object other code and other tests read.
    """
    from grant_watch.slack import conversation, tools

    conversation._cached_tools()
    conversation._cached_tools()
    assert not any("cache_control" in schema for schema in tools.TOOL_SCHEMAS)
    assert len(conversation._cached_tools()) == len(tools.TOOL_SCHEMAS)
