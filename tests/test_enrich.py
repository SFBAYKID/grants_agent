"""Contact-enrichment honesty (Constitution rule 1): an unreachable source must NEVER
be recorded as not_found, and a genuine not_found must be recorded truthfully. All
offline — finder's network calls are monkeypatched, no Firecrawl/Anthropic hit."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from grant_watch import db
from grant_watch.enrich import evidence, finder, firecrawl_gateway
from grant_watch.enrich.finder import ContactCandidate, SourceUnreachable
from grant_watch.models import FundingEventType, Lead, LeadGrade, RawItem
from grant_watch.slack import tools
from tests.contact_support import verified_contact_evidence
from tests.paid_provider_support import configure_firecrawl_runtime


def _lead(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    """One award lead to enrich against."""
    conn = db.connect(tmp_path / "t.db")
    db.upsert_lead(
        conn,
        Lead(
            item=RawItem(
                source="usaspending:16.071",
                item_id="A1",
                title="SVPP",
                entity="Castle Rock School District 401",
                state="WA",
                program="SVPP",
                amount=500_000.0,
                start="2025-10-01",
                end="2028-09-30",
                url="https://x.gov/a",
                raw={},
                event_type=FundingEventType.AWARD_OBLIGATED,
            ),
            grade=LeadGrade.GOLD,
        ),
    )
    return conn, int(conn.execute("SELECT id FROM leads").fetchone()["id"])


@pytest.mark.parametrize(
    "entity, kind",
    [
        ("City of East Providence", "city"),
        ("City of Salmon", "city"),
        ("Town of Kemah", "city"),
        ("Jefferson County", "city"),
        ("Tallapoosa Co School District", "school"),
        ("Alief ISD", "school"),
        ("Birmingham Community Charter High School", "school"),
        ("Dekalb County School District", "school"),  # school words win the tie
        ("Mars Hill Bible School", "school"),
    ],
)
def test_org_kind_classifies_city_vs_school(entity: str, kind: str) -> None:
    """City awards must not be treated as schools when picking a contact."""
    assert finder._org_kind(entity) == kind
    titles = finder._titles_for(entity)
    if kind == "city":
        assert "city manager" in titles and "superintendent" not in titles
    else:
        assert "superintendent" in titles


def test_linkedin_person_targets_city_roles_and_skips_school_people(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A city lead searches city roles and never attaches a school person.

    Live 2026-07-18: East Providence (a city award) surfaced the school district's
    IT director; a city award should reach a city official instead."""
    captured: dict[str, str] = {}

    def _fake_search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Return a school person first, then a city official."""
        captured["query"] = query
        return [
            {
                "url": "https://www.linkedin.com/in/sam-super",
                "title": "Sam Super - Superintendent - East Providence "
                "School District | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/pat-citymgr",
                "title": "Pat Manager - City Manager - City of East "
                "Providence | LinkedIn",
            },
        ]

    monkeypatch.setattr(finder, "_search", _fake_search)
    out = finder.linkedin_person("City of East Providence", "RI")
    assert "city manager" in captured["query"].lower()
    assert out is not None
    assert out["name"] == "Pat Manager"  # the school superintendent was skipped


def test_linkedin_person_rejects_role_titled_card_over_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A title-led card ('IT Director - City of Kemah') must never become a person
    named 'IT Director'; a later real-person card is preferred (H2)."""

    def _fake_search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """A role-titled result first, then a genuine person."""
        return [
            {
                "url": "https://www.linkedin.com/in/kemah-it",
                "title": "IT Director - City of Kemah | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/jane-doe",
                "title": "Jane Doe - City Manager - City of Kemah | LinkedIn",
            },
        ]

    monkeypatch.setattr(finder, "_search", _fake_search)
    out = finder.linkedin_person("City of Kemah", "TX")
    assert out is not None
    assert out["name"] == "Jane Doe"  # the role-titled card was rejected, not split


def test_linkedin_person_returns_none_when_only_role_cards_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every result is a role/org card, return None rather than a fabricated
    person Lead (H2)."""

    def _fake_search(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Only role/org-titled cards, no real person name."""
        return [
            {
                "url": "https://www.linkedin.com/in/kemah-it",
                "title": "IT Director - City of Kemah | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/kemah-pw",
                "title": "Public Works Director - City of Kemah | LinkedIn",
            },
        ]

    monkeypatch.setattr(finder, "_search", _fake_search)
    assert finder.linkedin_person("City of Kemah", "TX") is None


def test_find_contact_reports_org_address_for_linkedin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LinkedIn-only result still reports the org address that was enriched.

    Live bug 2026-07-18: City of Salmon / East Providence had the address stored
    (200 Main Street / 145 Taunton Ave.) but the reply said "no mailing address"."""
    from grant_watch.slack.contact_enrichment import ContactOutcome

    monkeypatch.setattr(
        tools.db, "connect", lambda *_a, **_k: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(
        tools,
        "enrich_lead_contact",
        lambda *_a, **_k: ContactOutcome(
            "linkedin_only",
            "Jane Roe",
            "IT Director",
            "",
            "",
            "https://www.linkedin.com/in/jane-roe",
            "",
            (
                " From the organization's website I also added phone 208-756-3214; "
                "address 200 Main Street, Salmon, 83467."
            ),
        ),
    )
    out = tools.find_contact(3035)
    assert "Jane Roe" in out  # the LinkedIn person is still reported
    assert "200 Main Street, Salmon, 83467" in out  # the address is no longer dropped


def test_scrape_keeps_footer_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scrape requests full-page content (onlyMainContent=false) so an org's
    address / general email / phone in the FOOTER are not dropped (live 2026-07-18:
    City of Melrose's street address was stripped by Firecrawl main-content mode)."""
    captured: dict[str, object] = {}

    class _Resp:
        """Minimal Firecrawl response stub."""

        def raise_for_status(self) -> None:
            """No HTTP error."""

        def json(self) -> dict[str, object]:
            """Return markdown that includes footer text."""
            return {"data": {"markdown": "562 Main Street, Melrose, MA 02176"}}

    def _fake_post(
        _url: str,
        headers: object = None,
        json: dict[str, object] | None = None,
        timeout: int = 0,
        stream: bool = False,
    ) -> _Resp:
        """Capture the request body Firecrawl would receive."""
        assert stream is True
        captured.update(json or {})
        return _Resp()

    configure_firecrawl_runtime(tmp_path, monkeypatch, limit=5)
    monkeypatch.setattr(firecrawl_gateway.requests, "post", _fake_post)
    conn = db.connect(tmp_path / "scrape.db")
    with firecrawl_gateway.bind_connection(conn, "test_scrape"):
        out = finder._scrape("https://cityofmelrose.org/", conn=conn)
    assert captured["onlyMainContent"] is False
    assert "562 Main Street" in out


# ------------------------------------------------------------ finder: reach vs not-found
def test_finder_raises_unreachable_when_search_never_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every search angle erroring means we could not look — raise, don't return None."""

    def boom(*_a: object, **_k: object) -> list[dict]:
        """Provide test-local behavior for boom."""
        raise SourceUnreachable("down")

    monkeypatch.setattr(finder, "_search", boom)
    with pytest.raises(SourceUnreachable):
        finder.find_contact("Castle Rock School District", "WA")


def test_finder_raises_unreachable_when_no_page_is_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search works but every page is blocked/empty — still 'could not look'."""
    monkeypatch.setattr(
        finder,
        "_search",
        lambda *_a, **_k: [
            {
                "url": "https://crschools.org/staff",
                "title": "Castle Rock School District staff — Washington",
            }
        ],
    )
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: "")  # blocked page
    with pytest.raises(SourceUnreachable):
        finder.find_contact("Castle Rock School District", "WA")


def test_general_mailbox_reverification_keeps_outage_distinct_from_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed evidence-page read raises unavailable instead of saying removed."""

    def timeout(*_args: object, **_kwargs: object) -> str:
        """Model an evidence-page request whose result is unavailable."""
        raise SourceUnreachable("offline")

    monkeypatch.setattr(finder, "_scrape", timeout)
    with pytest.raises(SourceUnreachable):
        finder.reverify_general_mailbox(
            "security@crschools.org",
            "https://crschools.org/contact",
            "crschools.org",
        )


def test_finder_returns_none_when_pages_read_but_nothing_verifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real page that yields no verifiable contact is a TRUTHFUL not_found (None)."""
    monkeypatch.setattr(
        finder,
        "_search",
        lambda *_a, **_k: [
            {
                "url": "https://crschools.org/staff",
                "title": "Castle Rock School District staff — Washington",
            }
        ],
    )
    monkeypatch.setattr(finder, "_scrape", lambda *_a, **_k: "x" * 400)  # real content
    monkeypatch.setattr(finder, "_extract", lambda *_a, **_k: None)  # clean negative
    assert finder.find_contact("Castle Rock School District", "WA") is None


def test_contact_fields_require_independent_page_evidence() -> None:
    """A verified email cannot smuggle an invented title or phone into storage."""
    page = "Jane Doe — jdoe@crschools.org — Technology Director — (360) 555-0100"
    assert finder._text_field_on_page(page, "Technology Director") is True
    assert finder._text_field_on_page(page, "Chief Security Officer") is False
    assert finder._phone_on_page(page, "360-555-0100") is True
    assert finder._phone_on_page(page, "360-555-9999") is False


def test_phone_digits_from_unrelated_fields_never_combine() -> None:
    """Only a phone-shaped span can prove a number."""
    page = "Office code 415. Case 555. Room 1212."
    assert finder._phone_on_page(page, "(415) 555-1212") is False


@pytest.mark.parametrize(
    ("page", "value"),
    [
        ("The organization has offices", "OR"),
        ("Services are mainly online", "Main"),
        ("Contact the New Yorker office", "York"),
    ],
)
def test_text_evidence_requires_whole_tokens(page: str, value: str) -> None:
    """Short state/address/city strings cannot hide inside unrelated words."""
    assert finder._text_field_on_page(page, value) is False


def test_search_result_must_bind_to_named_entity() -> None:
    """A directory/near-name result cannot become the organization's official site."""
    assert (
        finder._looks_official(
            "Castle Rock School District",
            "WA",
            {
                "url": "https://crschools.org/staff",
                "title": "Castle Rock School District staff — Washington",
            },
        )
        is True
    )
    assert (
        finder._looks_official(
            "Orange Unified School District",
            "CA",
            {
                "url": "https://orangecountywater.example/staff",
                "title": "Orange County Water Authority",
            },
        )
        is False
    )


def test_search_result_does_not_treat_ordinary_or_as_oregon() -> None:
    """A lowercase conjunction is not an exact USPS state-code claim."""
    assert (
        finder._looks_official(
            "Springfield Public Schools",
            "OR",
            {
                "url": "https://springfield.example/staff",
                "title": "Springfield Public Schools staff or departments",
            },
        )
        is False
    )
    assert (
        finder._looks_official(
            "Springfield Public Schools",
            "CA",
            {
                "url": "https://springfield.example/staff",
                "title": "Springfield Public Schools — Massachusetts",
            },
        )
        is False
    )


def test_search_result_accepts_an_exact_uppercase_usps_code() -> None:
    """A real uppercase code remains valid while lowercase words stay invalid."""
    assert finder._looks_official(
        "Alpha School District",
        "CA",
        {
            "url": "https://alpha.example/staff",
            "title": "Alpha School District — CA",
        },
    )


# ------------------------------------------------------------ enrich_lead_contact honesty
def test_enrich_unreachable_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage returns 'unreachable' and writes NO contact row — a retry can re-look."""
    conn, lead_id = _lead(tmp_path)

    def raise_unreachable(*_a: object, **_k: object) -> ContactCandidate:
        """Provide test-local behavior for raise unreachable."""
        raise SourceUnreachable("down")

    monkeypatch.setattr(finder, "find_contact", raise_unreachable)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "unreachable"
    assert (
        db.contacts_for_lead(conn, lead_id) == []
    )  # nothing fabricated, nothing final


def test_org_enrichment_summary_logs_unreachable_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable org site is an EXPECTED retryable non-result: return '' and log a
    clean one-liner — never the alarming [tool-error] traceback that reads like a code bug
    (live 2026-07-20: WICHITA FALLS ISD's unreachable site logged a full traceback)."""
    from grant_watch.enrich import organization_profile

    def _unreachable(*_a: object, **_k: object) -> object:
        """Stand in for a site that could not be read."""
        raise SourceUnreachable("could not read any page for WICHITA FALLS ISD")

    monkeypatch.setattr(organization_profile, "enrich_org_profile", _unreachable)
    assert organization_profile.org_enrichment_summary(None, 1) == ""
    err = capsys.readouterr().err
    assert "[tool-error]" not in err  # no alarming error marker
    assert "Traceback" not in err  # no full traceback for an expected condition
    assert "unreachable" in err.lower()  # the retryable non-result is still noted


def test_org_enrichment_summary_still_traces_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A genuinely UNEXPECTED exception still returns '' but keeps the full traceback, so a
    real bug is never silently swallowed by the clean-up above."""
    from grant_watch.enrich import organization_profile

    def _boom(*_a: object, **_k: object) -> object:
        """Stand in for an unexpected code fault."""
        raise ValueError("unexpected org-enrichment fault")

    monkeypatch.setattr(organization_profile, "enrich_org_profile", _boom)
    assert organization_profile.org_enrichment_summary(None, 999) == ""
    err = capsys.readouterr().err
    assert "[tool-error] org_enrichment_summary" in err  # unexpected → loud
    assert "Traceback" in err


def _stub_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    person: dict[str, str] | None,
    general_email: str,
) -> None:
    """Script the fallback chain: LinkedIn person and org-profile mailbox."""
    from grant_watch.enrich import organization_profile

    monkeypatch.setattr(finder, "linkedin_person", lambda *_a, **_k: person)
    profile = organization_profile.OrgProfile(
        general_email=general_email,
        source_url="https://example.org/contact" if general_email else "",
        status="found" if general_email else "not_found",
    )
    monkeypatch.setattr(
        organization_profile, "enrich_org_profile", lambda *_a, **_k: profile
    )


def test_enrich_genuine_miss_records_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only after site, LinkedIn, AND org mailbox all miss is the lead not_found."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    _stub_fallbacks(monkeypatch, person=None, general_email="")
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "not_found"
    rows = db.contacts_for_lead(conn, lead_id)
    assert len(rows) == 1 and rows[0]["contact_status"] == "not_found"
    assert rows[0]["email"] is None


def test_linkedin_outage_cannot_become_permanent_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean site miss plus an unavailable LinkedIn search remains retryable."""
    from grant_watch.enrich import organization_profile

    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)

    def _linkedin_down(*_a: object, **_k: object) -> None:
        """Represent a temporary provider outage, not a negative result."""
        raise SourceUnreachable("search unavailable")

    monkeypatch.setattr(finder, "linkedin_person", _linkedin_down)
    monkeypatch.setattr(
        organization_profile,
        "enrich_org_profile",
        lambda *_a, **_k: organization_profile.OrgProfile(status="not_found"),
    )
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "unreachable"
    assert db.contacts_for_lead(conn, lead_id) == []


def test_org_outage_cannot_become_permanent_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean LinkedIn miss plus an unavailable org site remains retryable."""
    from grant_watch.enrich import organization_profile

    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    monkeypatch.setattr(finder, "linkedin_person", lambda *_a, **_k: None)

    def _org_down(*_a: object, **_k: object) -> None:
        """Represent a temporary website outage, not a negative result."""
        raise SourceUnreachable("organization site unavailable")

    monkeypatch.setattr(organization_profile, "enrich_org_profile", _org_down)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "unreachable"
    assert db.contacts_for_lead(conn, lead_id) == []


def test_positive_linkedin_result_survives_org_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An independent positive remains usable when the other fallback is down."""
    from grant_watch.enrich import organization_profile

    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    monkeypatch.setattr(
        finder,
        "linkedin_person",
        lambda *_a, **_k: {
            "name": "Dana Roe",
            "title": "Technology Director",
            "url": "https://www.linkedin.com/in/dana-roe",
        },
    )

    def _org_down(*_a: object, **_k: object) -> None:
        """Represent a temporary website outage."""
        raise SourceUnreachable("organization site unavailable")

    monkeypatch.setattr(organization_profile, "enrich_org_profile", _org_down)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "linkedin_only"
    assert outcome.name == "Dana Roe"


def test_single_contact_request_runs_org_enrichment_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendering a Slack result must not trigger a second paid organization pass."""
    from grant_watch.enrich import organization_profile

    conn, lead_id = _lead(tmp_path)
    calls = 0
    monkeypatch.setattr(tools.db, "connect", lambda *_a, **_k: conn)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    monkeypatch.setattr(finder, "linkedin_person", lambda *_a, **_k: None)

    def _profile(*_a: object, **_k: object) -> organization_profile.OrgProfile:
        """Return one verified mailbox while counting actual enrichment passes."""
        nonlocal calls
        calls += 1
        return organization_profile.OrgProfile(
            website="https://crschools.org",
            general_email="info@crschools.org",
            source_url="https://crschools.org/contact",
            status="found",
        )

    monkeypatch.setattr(organization_profile, "enrich_org_profile", _profile)
    rendered = tools.find_contact(lead_id)
    assert "info@crschools.org" in rendered
    assert calls == 1


def test_enrich_falls_back_to_linkedin_and_org_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No on-site person -> a LinkedIn name plus the org's general mailbox.

    Chase's rule: every school and city has an email somewhere — a bare
    not_found without trying LinkedIn and the org mailbox is a failed lookup."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    _stub_fallbacks(
        monkeypatch,
        person={
            "name": "Dana Roe",
            "title": "Technology Director",
            "url": "https://www.linkedin.com/in/dana-roe",
        },
        general_email="info@example.org",
    )
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "linkedin_org_email"
    assert outcome.name == "Dana Roe"
    assert outcome.email == "info@example.org"
    saved = db.contacts_for_lead(conn, lead_id)
    assert any(c["contact_status"] == "linkedin_only" for c in saved)


def test_enrich_falls_back_to_org_mailbox_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No person anywhere -> the org's verified general mailbox, clearly labeled."""
    conn, lead_id = _lead(tmp_path)
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: None)
    _stub_fallbacks(monkeypatch, person=None, general_email="office@example.org")
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "org_email"
    assert outcome.email == "office@example.org"
    assert outcome.name == ""
    # Not marked not_found: a usable mailbox was honestly found.
    rows = db.contacts_for_lead(conn, lead_id)
    assert not any(c["contact_status"] == "not_found" for c in rows)


def test_enrich_verified_saves_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified candidate is persisted and returned with its real fields."""
    conn, lead_id = _lead(tmp_path)
    source_url = "https://crschools.org/staff"
    page = "Jane Doe — Technology Director — jdoe@crschools.org"
    field_evidence = finder._contact_identity_evidence(
        page, "jdoe@crschools.org", "Jane Doe", source_url
    )
    title_evidence = finder.evidence.phrase(
        page, "Technology Director", source_url, field="title"
    )
    assert title_evidence is not None
    field_evidence["title"] = title_evidence
    cand = ContactCandidate(
        name="Jane Doe",
        title="Technology Director",
        email="jdoe@crschools.org",
        phone="",
        source_url=source_url,
        confidence="high",
        field_evidence=field_evidence,
    )
    monkeypatch.setattr(finder, "find_contact", lambda *_a, **_k: cand)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "verified" and outcome.email == "jdoe@crschools.org"
    rows = db.contacts_for_lead(conn, lead_id)
    assert rows[0]["contact_status"] == "verified"
    assert rows[0]["email"] == "jdoe@crschools.org"
    stored_evidence = json.loads(str(rows[0]["field_evidence_json"]))
    assert set(stored_evidence) == {"email", "name", "title"}
    assert stored_evidence["email"]["source_url"] == source_url
    assert len(stored_evidence["email"]["evidence_hash"]) == 64


def test_contact_persistence_rejects_untyped_field_evidence(tmp_path: Path) -> None:
    """A boolean cannot be persisted as if it were auditable page evidence."""
    conn, lead_id = _lead(tmp_path)
    with pytest.raises(ValueError, match="lacks typed evidence"):
        db.save_contact(
            conn,
            lead_id,
            "Jane Doe",
            "",
            "jdoe@crschools.org",
            "",
            "https://crschools.org/staff",
            "high",
            field_evidence={"email": True},
        )


def test_enrich_reuses_existing_verified_without_researching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-verified contact is returned as-is; finder is never called again."""
    conn, lead_id = _lead(tmp_path)
    db.save_contact(
        conn,
        lead_id,
        "Sam Smith",
        "IT Director",
        "ssmith@crschools.org",
        "",
        "https://crschools.org/it",
        "high",
        field_evidence=verified_contact_evidence(
            "Sam Smith",
            "ssmith@crschools.org",
            "https://crschools.org/it",
            title="IT Director",
        ),
    )

    def fail(*_a: object, **_k: object) -> ContactCandidate:
        """Provide test-local behavior for fail."""
        raise AssertionError("finder must not run when a verified contact exists")

    monkeypatch.setattr(finder, "find_contact", fail)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "verified" and outcome.email == "ssmith@crschools.org"


def test_second_pass_over_a_fallback_outcome_reports_it_instead_of_erroring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-enriched lead must report what the first pass found, never "error".

    The paid-attempt ledger is per lead, but only the `verified` and `not_found`
    endings short-circuit ahead of it. A lead whose first pass ended in a FALLBACK
    outcome therefore reached the ledger again on every later pass, raised
    CompletedPaidCall, and rendered as a bare "error" cell in the search grid —
    reporting failure for enrichment that had genuinely succeeded.

    The second pass here installs stubs that RAISE, so any attempt to redo the paid
    discovery fails the test rather than silently passing.
    """
    from grant_watch.enrich import organization_profile

    conn, lead_id = _lead(tmp_path)

    monkeypatch.setattr(finder, "find_contact", lambda *a, **k: None)
    monkeypatch.setattr(
        finder,
        "linkedin_person",
        lambda *a, **k: {
            "name": "Dana Reyes",
            "title": "Director of Technology",
            "url": "https://linkedin.com/in/danareyes",
        },
    )
    monkeypatch.setattr(
        organization_profile,
        "enrich_org_profile",
        lambda *a, **k: organization_profile.OrgProfile(status="not_found"),
    )
    first = tools.enrich_lead_contact(conn, lead_id)
    assert first.status == "linkedin_only"
    assert first.name == "Dana Reyes"

    def _must_not_run(*args: object, **kwargs: object) -> None:
        """Fail the test if the paid discovery chain is re-entered."""
        raise AssertionError("the paid discovery chain must not run a second time")

    monkeypatch.setattr(finder, "find_contact", _must_not_run)
    monkeypatch.setattr(finder, "linkedin_person", _must_not_run)
    monkeypatch.setattr(organization_profile, "enrich_org_profile", _must_not_run)
    second = tools.enrich_lead_contact(conn, lead_id)
    assert second.status == "linkedin_only"
    assert second.name == "Dana Reyes"
    assert second.source_url == "https://linkedin.com/in/danareyes"
    conn.close()


def test_completed_legacy_positive_without_typed_evidence_is_not_a_false_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quarantined positive plus completed marker requires operator reconciliation."""
    conn, lead_id = _lead(tmp_path)
    conn.execute(
        """INSERT INTO paid_enrichment_attempts
             (id,lead_id,operation,request_key,attempt_no,state,started_at,finished_at)
           VALUES ('legacy-positive',?,'legacy_contact_enrichment',?,1,'completed',
                   '2026-08-01','2026-08-01')""",
        (lead_id, f"legacy-contact:{lead_id}"),
    )
    conn.commit()

    def _must_not_run(*_args: object, **_kwargs: object) -> None:
        """Completed accounting must not silently re-enter provider discovery."""
        raise AssertionError("provider discovery must not run")

    monkeypatch.setattr(finder, "find_contact", _must_not_run)
    outcome = tools.enrich_lead_contact(conn, lead_id)
    assert outcome.status == "needs_operator_retry"
    assert db.contacts_for_lead(conn, lead_id) == []


def test_second_pass_recalls_an_org_mailbox_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The org-mailbox ending is reconstructed from the persisted profile columns."""
    from grant_watch.enrich import organization_profile

    conn, lead_id = _lead(tmp_path)

    monkeypatch.setattr(finder, "find_contact", lambda *a, **k: None)
    monkeypatch.setattr(finder, "linkedin_person", lambda *a, **k: None)

    def _write_profile(
        conn_: sqlite3.Connection, lid: int, *a: object
    ) -> organization_profile.OrgProfile:
        """Persist the org profile the real enricher would write, then report it."""
        general_email = "info@crsd401.test"
        source_url = "https://crsd401.test/contact"
        email_evidence = evidence.exact_email(
            f"Contact us at {general_email}",
            general_email,
            source_url,
            field="general_email",
        )
        assert email_evidence is not None
        profile = organization_profile.OrgProfile(
            general_email=general_email,
            source_url=source_url,
            status="found",
            field_evidence={"general_email": email_evidence},
        )
        db.save_org_profile(conn_, lid, profile)
        return profile

    monkeypatch.setattr(organization_profile, "enrich_org_profile", _write_profile)
    assert tools.enrich_lead_contact(conn, lead_id).status == "org_email"

    def _must_not_run(*args: object, **kwargs: object) -> None:
        """Fail the test if the paid discovery chain is re-entered."""
        raise AssertionError("the paid discovery chain must not run a second time")

    monkeypatch.setattr(finder, "find_contact", _must_not_run)
    monkeypatch.setattr(organization_profile, "enrich_org_profile", _must_not_run)
    second = tools.enrich_lead_contact(conn, lead_id)
    assert second.status == "org_email"
    assert second.email == "info@crsd401.test"
    conn.close()
