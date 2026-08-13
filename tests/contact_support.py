"""Typed page-evidence builders shared by contact workflow tests.

Tests must cross the same persistence boundary as production. This helper creates a
small deterministic page, runs the real token verifiers, and returns their typed
matches; it never fabricates a ``verified`` database row with a bare status string.
"""

from __future__ import annotations

from grant_watch.enrich import evidence


def verified_contact_evidence(
    name: str,
    email: str,
    source_url: str,
    *,
    title: str = "",
    phone: str = "",
) -> dict[str, evidence.EvidenceMatch]:
    """Return exact same-line evidence for one test contact and optional facts."""
    page = " — ".join(value for value in (name, title, email, phone) if value)
    email_match = evidence.exact_email(page, email, source_url)
    assert email_match is not None
    name_match = evidence.person_name_near_email(page, name, email_match)
    assert name_match is not None
    matches = {"email": email_match, "name": name_match}
    if title:
        title_match = evidence.phrase(page, title, source_url, field="title")
        assert title_match is not None
        matches["title"] = title_match
    if phone:
        phone_match = evidence.phone(page, phone, source_url)
        assert phone_match is not None
        matches["phone"] = phone_match
    return matches
