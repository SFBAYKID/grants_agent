"""Evidence rules and Salesforce field selection for one Grant contact.

Split out of `salesforce_contact_records` at the 1000-line cap (rule 4). The
responsibility is narrow and worth isolating: everything here is a PURE function of
a `leads` row and a `contacts` row that decides what may honestly be written into a
provenance-free CRM field — which title may be used, whether an address is a
person's or the organization's, and what evidence sentence describes the source.
The approval/preview/confirm workflow that consumes these lives next door.
"""

from __future__ import annotations

import re
import sqlite3

from ..presentation import strip_leading_honorifics
from ..record_semantics import RecordKind, semantics_for

# Contact statuses that may back a Salesforce record. Website-verified contacts
# carry a verbatim on-page email; linkedin_only rows carry a profile URL and no
# email, and every rendering must say the profile's ownership is unverified.
_USABLE_CONTACT_STATUSES = ("verified", "linkedin_only")
# Statuses whose `title` may be written into Salesforce's provenance-free `Title`
# field. `linkedin_only` is deliberately absent — see title_is_writable.
_TITLE_BEARING_STATUSES = frozenset({"verified", "vendor_licensed"})


def split_person_name(name: str) -> tuple[str, str]:
    """Split a contact's full name into (FirstName, LastName); never guess.

    A single token becomes the LastName with a blank FirstName — Salesforce
    requires LastName and Grant does not invent given names. A leading honorific
    (Mr./Mrs./Dr./…) is dropped so it never becomes part of the FirstName."""
    tokens = strip_leading_honorifics(name).split()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return " ".join(tokens[:-1]), tokens[-1]


def _amount_text(row: sqlite3.Row) -> str:
    """Render the award amount without ever inventing one."""
    amount = row["amount"]
    if amount is None or float(amount) <= 0:
        return "amount not recorded"
    return f"${float(amount):,.0f}"


def grant_summary(row: sqlite3.Row) -> str:
    """One honest sentence describing the record behind this Salesforce note.

    Wording follows the record kind, never the grade. This note is CREATE-ONLY and
    permanent: describing a solicitation's response deadline as an award's spend window
    would write a false fact into the CRM the reps trust, with no way to correct it.
    """
    program = str(row["program"] or "unlabeled program")
    meaning = semantics_for(row)
    source = str(row["detail_url"] or "not provided")
    window = f"{row['funds_start'] or 'unknown'} to {row['funds_end'] or 'unknown'}"
    if meaning.asserts_award:
        return (
            f"{_amount_text(row)} {program} grant; spend window {window}. "
            f"Grant source {source}."
        )
    if meaning.kind is RecordKind.SOLICITATION:
        return (
            f"{program} solicitation; response due "
            f"{row['funds_end'] or 'unknown'}. Source {source}."
        )
    if meaning.kind is RecordKind.FUNDING_OPPORTUNITY:
        return f"{program} funding opportunity; applications {window}. Source {source}."
    return f"{program} public funding record; dates unverified. Source {source}."


_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _month_year(value: object) -> str:
    """Render an ISO date as 'Oct 2025'; fall back to the raw value if unparseable."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"(\d{4})-(\d{2})", text)
    if not match:
        return text
    year, month = match.group(1), int(match.group(2))
    if 1 <= month <= 12:
        return f"{_MONTHS[month - 1]} {year}"
    return text


def _spend_window(row: sqlite3.Row) -> str:
    """Human 'spend window Oct 2025 – Sep 2028' clause, or '' when unverifiable.

    Returns '' unless the record IS an award — a solicitation has no spend window, and
    labelling its response deadline as one is a permanent false CRM fact.
    """
    meaning = semantics_for(row)
    if not meaning.asserts_award:
        return ""
    start, end = _month_year(row["funds_start"]), _month_year(row["funds_end"])
    if start and end:
        return f"spend window {start} – {end}"
    if start:
        return f"spend window opens {start}"
    if end:
        return f"spend window through {end}"
    return ""


def _grant_headline(row: sqlite3.Row) -> str:
    """Compact 'SVPP · $500,000 · spend window Oct 2025 – Sep 2028' summary line."""
    meaning = semantics_for(row)
    # The amount rides along ONLY when the kind establishes it as awarded money. A bare
    # "SVPP · $487,657" in a create-only CRM note reads as an award no matter what the
    # body says, and the note cannot be corrected afterwards.
    parts = [str(row["program"] or meaning.noun)]
    if meaning.asserts_amount:
        parts.append(_amount_text(row))
    window = _spend_window(row)
    if window:
        parts.append(window)
    return " · ".join(parts)


def _contact_evidence(contact: sqlite3.Row) -> str:
    """Describe where the contact came from, honestly per evidence kind.

    THIS SENTENCE IS WRITTEN INTO A SALESFORCE RECORD and outlives every thread, so
    it is the most durable claim Grant makes about a person. It used to special-case
    only `linkedin_only` and fall through to "Contact verified verbatim on {source}"
    for everything else — which meant a ZoomInfo contact, with no source URL at all,
    was filed as "Contact verified verbatim on unknown source". A claim of
    verification, citing nothing, about data nobody checked.

    Every evidence class now says what it actually is. `contact_status` is preferred
    over `provenance` only where the two can disagree on legacy rows; both are read
    so a row written before the provenance split is still described correctly.
    """
    source = str(contact["source_url"] or "").strip()
    status = str(contact["contact_status"] or "")
    provenance = _row_value(contact, "provenance")
    # DO-NOT-CALL HAS TO TRAVEL WITH THE PERSON. Until now the flag was enforced only
    # at storage time — the number is blanked before it is written locally — and then
    # nothing carried it any further. That is airtight while the number stays inside
    # Grant, and worth nothing the moment a rep obtains it another way: the Salesforce
    # record names a real person with no marker at all, and there is no Salesforce
    # field being populated with it either. An empty Phone reads as "we don't have it",
    # not as "do not call this person".
    #
    # Saying it in the Description is not as good as a real DoNotCall checkbox, and it
    # is what this integration user can actually write today. It appears FIRST because
    # a compliance fact a rep has to scroll for is a compliance fact they will miss.
    dnc = (
        "DO NOT CALL: this person is flagged do-not-call; any number for them must not be dialled. "
        if _row_value(contact, "do_not_call") in ("1", 1, True, "True")
        else ""
    )

    if status == "linkedin_only" or provenance == "linkedin_claimed":
        where = source or "a LinkedIn profile"
        return f"{dnc}Evidence is a LinkedIn profile (ownership not verified): {where}."
    if status == "vendor_licensed" or provenance == "vendor_licensed":
        return (
            f"{dnc}Supplied by ZoomInfo from licensed data. Grant did NOT verify "
            "this against the organization's own site."
        )
    if status == "human_asserted" or provenance == "human_asserted":
        who = _row_value(contact, "asserted_by_slack_user")
        when = _row_value(contact, "asserted_at")[:10]
        attribution = f" by {who}" if who else ""
        dated = f" on {when}" if when else ""
        return (
            f"Supplied{attribution}{dated} by a Monarch rep in Slack, and recorded "
            "as their statement. Not independently verified by Grant."
        )
    if not source:
        # Never claim verification without being able to say against what.
        return "Contact recorded by Grant; no source page was captured."
    return f"Contact verified verbatim on {source}."


def _row_value(row: sqlite3.Row, column: str) -> str:
    """Read an optional column that may be absent on a legacy row."""
    try:
        return str(row[column] or "")
    except (IndexError, KeyError):
        return ""


# The Lead record type Grant's leads belong to (resolved by DeveloperName at
# runtime; this is the org default and the correct type for these prospects).
LEAD_RECORD_TYPE = "Verkada"
_SCHOOL_RE = re.compile(
    r"\b(school|schools|district|academy|isd|usd|elementary|k-?12|charter)\b",
    re.IGNORECASE,
)
_CITY_RE = re.compile(
    r"\b(city of|town of|village of|county|municipal)\b", re.IGNORECASE
)


def _lead_value(lead: sqlite3.Row, column: str) -> str:
    """Safely read an optional lead column that may be absent on legacy rows."""
    try:
        return str(lead[column] or "")
    except (IndexError, KeyError):
        return ""


# Mailbox local-parts that belong to an ORGANIZATION, never to one person. A page
# that lists `office@ygla.org` beside a named director is giving the switchboard,
# not that director's address.
_GENERAL_MAILBOXES = frozenset(
    {
        "info",
        "office",
        "admin",
        "administration",
        "contact",
        "hello",
        "mail",
        "main",
        "general",
        "reception",
        "frontdesk",
        "front-desk",
        "enquiries",
        "inquiries",
        "support",
        "help",
        "webmaster",
        "postmaster",
        "noreply",
        "no-reply",
        "district",
        "school",
        "secretary",
    }
)


def email_is_general(email: str) -> bool:
    """Whether this address is an organization mailbox rather than one person's."""
    local = email.strip().lower().partition("@")[0]
    return bool(local) and local.split("+")[0] in _GENERAL_MAILBOXES


def title_is_writable(contact: sqlite3.Row, entity: str) -> bool:
    """Whether this contact's title may become a Salesforce `Title` field.

    A Salesforce `Title` carries NO provenance. Once written, nobody downstream can
    tell a title verified verbatim on a district page from a line scraped off a
    LinkedIn profile whose ownership was never established — and `salesforce_lead_fill`
    already refuses the latter for exactly that reason, in a comment that says so.
    This path did not, so on 2026-08-11 it created a Salesforce Lead whose job title
    is "Corkscrew Salesman and Finder". The LinkedIn finding is still shown in Slack,
    where it is labelled as unverified; it just stops becoming a CRM fact.
    """
    from .. import db  # local import: mirrors this module's cycle-avoiding style

    title = str(contact["title"] or "").strip()
    if not title:
        return False
    if str(contact["contact_status"] or "") not in _TITLE_BEARING_STATUSES:
        return False
    # A "title" that is just the organization name is not a person's role.
    return (
        db.canonical_entity_key(title).partition("|")[0]
        != db.canonical_entity_key(entity).partition("|")[0]
    )


def choose_email(contact: sqlite3.Row, lead: sqlite3.Row) -> tuple[str, str]:
    """Pick the best verified email and label its kind: direct | general | ''.

    A person's verbatim-verified email is preferred; otherwise the organization's
    verified general mailbox (info@/office@) is used and clearly labeled so Grant
    never implies a general address is the individual's.

    THE LABEL USED TO DEPEND ON WHICH COLUMN THE ADDRESS CAME FROM, not on the
    address. `office@ygla.org` scraped onto the CONTACT row was announced to a rep
    as "Email (direct): office@ygla.org" on 2026-08-11 — the exact claim the
    docstring above says this function exists to prevent, because the general branch
    only fired when the contact had no email at all.
    """
    if contact["email"]:
        email = str(contact["email"])
        return email, "general" if email_is_general(email) else "direct"
    general = _lead_value(lead, "org_general_email")
    if general:
        return general, "general"
    return "", ""


def choose_phone(contact: sqlite3.Row, lead: sqlite3.Row) -> tuple[str, str]:
    """Pick the best verified phone and label its kind: direct | org_general | ''.

    The exact counterpart of choose_email, and it exists for the same reason. The
    payload already fell back to the organization's main line when a person had no
    direct number, but — unlike email — said nothing about it. An SDR opening that
    Lead dialled a district switchboard believing it was the named person's line,
    which is a claim no source supports. A LinkedIn-sourced person in particular
    never has a phone of their own, so the fallback fired on exactly the contacts
    whose identity was least verified.
    """
    if contact["phone"]:
        return str(contact["phone"]), "direct"
    # THE ORG FALLBACK IS ONLY AS GOOD AS THE LOOKUP THAT PRODUCED IT. A `not_found`
    # org profile still leaves `org_phone` holding whatever the failed search landed
    # on — the same defect that put `cde.ca.gov` in an `org_website` and nearly wrote
    # the state education department into five Salesforce Leads. This is a different
    # surface (the contact-record payloads) with identical shape, found by the
    # guardian while checking the first fix.
    if _lead_value(lead, "org_profile_status") != "found":
        return "", ""
    org_phone = _lead_value(lead, "org_phone")
    if org_phone:
        return org_phone, "org_general"
    return "", ""


def _infer_industry(lead: sqlite3.Row) -> str:
    """Infer the CRM Industry only when the org type is unambiguous, else ''."""
    entity = str(lead["entity_name"] or "")
    if _lead_value(lead, "nces_id") or _SCHOOL_RE.search(entity):
        return "K-12 Schools"
    if _CITY_RE.search(entity):
        return "Cities"
    return ""


def organization_fields(lead: sqlite3.Row) -> dict[str, object]:
    """Every ORGANIZATION fact Grant holds, ready for a Salesforce Lead payload.

    WHY THIS IS SHARED. Grant builds two kinds of Lead: a person Lead when a contact
    is verified, and an ORGANIZATION-ONLY Lead when none is. The person payload
    carried the address, website, student count and industry; the organization-only
    payload carried none of them, so the twelve org-only Leads written for the
    California campaign landed with an empty address and no firmographics — a record
    a rep cannot act on without going and researching it themselves, which is the
    work Grant exists to remove.

    None of these fields describe a PERSON, so nothing here depends on having found
    one. Every key is omitted unless the value is actually present: an absent address
    stays absent rather than becoming an empty string that looks filled in.
    """
    payload: dict[str, object] = {}
    state = str(lead["state"] or "") or _lead_value(lead, "org_state")
    if state:
        payload["State"] = state

    # A FAILED ORG LOOKUP STILL LEAVES THESE COLUMNS POPULATED, with whatever URL the
    # search happened to land on. Measured on production: two leads whose
    # `org_profile_status` is `not_found` carried `org_website='https://cde.ca.gov'`
    # — the California Department of Education, not the district — and a third
    # carried a CMS vendor's CDN. Authoritative-looking enough that no rep would
    # doubt it, and wrong.
    #
    # Worse in this direction than most bad writes, because the fill path only ever
    # writes into EMPTY fields: once `cde.ca.gov` lands in Website, that field is
    # closed to the tool forever and a later run after a fix skips it. The tool that
    # makes the error can never correct it — only a person can. So the profile's own
    # verdict gates every value derived from it.
    profile_found = _lead_value(lead, "org_profile_status") == "found"
    org_city = _lead_value(lead, "org_city") if profile_found else ""
    city = org_city or str(lead["location_city"] or "")
    if city:
        payload["City"] = city
    if profile_found:
        if _lead_value(lead, "org_street"):
            payload["Street"] = _lead_value(lead, "org_street")
        if _lead_value(lead, "org_postal_code"):
            payload["PostalCode"] = _lead_value(lead, "org_postal_code")
        if _lead_value(lead, "org_website"):
            payload["Website"] = _lead_value(lead, "org_website")
    enrollment = lead["enrollment"]
    if enrollment not in (None, "", 0):
        payload["Number_of_Students__c"] = int(enrollment)
    industry = _infer_industry(lead)
    if industry:
        payload["Industry"] = industry
    return payload
