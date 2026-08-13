"""Pure, field-specific evidence matching for fetched public documents.

Every accepted value is bound to one page and a bounded excerpt.  Verifiers work on
individual tokens or candidates; they never concatenate all page digits or accept a
substring embedded inside a different email, word, or postal code.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..state_codes import US_STATE_NAMES, normalize_state_code


VERIFIER_VERSION = "field-evidence-v2"
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9._%+\-])"
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[ .\-]?)?"
    r"(?:\(\d{3}\)|\d{3})[ .\-]\d{3}[ .\-]\d{4}"
    r"(?:\s*(?:x|ext\.?)\s*\d{1,6})?(?!\d)",
    re.IGNORECASE,
)
LOCAL_PHONE_RE = re.compile(
    r"(?<!\d)\d{3}[ .\-]\d{4}(?:\s*(?:x|ext\.?)\s*\d{1,6})?(?!\d)",
    re.IGNORECASE,
)
STREET_ADDRESS_RE = re.compile(
    r"(?<![A-Za-z0-9])\d{1,8}\s+"
    r"(?:[A-Za-z0-9.'#-]+\s+){0,8}"
    r"(?:street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|drive|dr|way|"
    r"court|ct|parkway|pkwy|highway|hwy|trail|trl|circle|cir|place|pl|"
    r"terrace|ter|plaza|plz|square|sq|loop|center|ctr|crossing|xing|"
    r"cove|cv|walk|mall)\b\.?(?![A-Za-z])",
    re.IGNORECASE,
)
ADDRESS_ANCHOR_RE = re.compile(
    r"(?:"
    + STREET_ADDRESS_RE.pattern
    + r"|\b(?:P\.?\s*O\.?|POST\s+OFFICE)\s+BOX\s+[A-Za-z0-9-]+"
    r"|(?<![A-Za-z0-9])\d{1,8}\s+(?:[A-Za-z]+\s+){0,4}"
    r"(?:COUNTY\s+|STATE\s+|U\.?S\.?\s+)?(?:ROUTE|RTE)\s+[A-Za-z0-9-]+"
    r"|\b(?:VENDOR\s+)?(?:REMITTANCE|MAILING|BILLING|SHIPPING|PAYMENT|"
    r"PHYSICAL|OFFICE|HEADQUARTERS|LOCATION|DISTRICT\s+OFFICE)"
    r"(?:\s+ADDRESS)?\s*:)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceMatch:
    """One value proven by a bounded fragment of one fetched page."""

    field: str
    value: str
    source_url: str
    excerpt: str
    evidence_hash: str
    verifier_version: str = VERIFIER_VERSION


def _bounded_excerpt(text: str, start: int, end: int, radius: int = 180) -> str:
    """Return compact surrounding evidence without persisting an entire page."""
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return " ".join(text[left:right].split())[:500]


def _line_excerpt(text: str, start: int, end: int) -> str:
    """Return the line/block containing a match, bounded around oversized markup."""
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    if right < 0:
        right = len(text)
    if right - left > 500:
        return _bounded_excerpt(text, start, end, radius=220)
    return " ".join(text[left:right].split())[:500]


def recorded_match(field: str, value: str, url: str, excerpt: str) -> EvidenceMatch:
    """Build a stable evidence record from a verified local excerpt."""
    payload = f"{VERIFIER_VERSION}\n{field}\n{value}\n{url}\n{excerpt}"
    return EvidenceMatch(
        field=field,
        value=value,
        source_url=url,
        excerpt=excerpt,
        evidence_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def _tokens(value: str) -> list[str]:
    """Return lowercase alphanumeric tokens for boundary-aware phrase matching."""
    return re.findall(r"[a-z0-9]+", value.lower())


def _token_span(text: str, wanted: list[str]) -> tuple[int, int] | None:
    """Locate one contiguous token sequence and return its original text span."""
    if not wanted:
        return None
    found = list(re.finditer(r"[A-Za-z0-9]+", text))
    lowered = [item.group(0).lower() for item in found]
    width = len(wanted)
    for index in range(len(lowered) - width + 1):
        if lowered[index : index + width] == wanted:
            return found[index].start(), found[index + width - 1].end()
    return None


def exact_email(
    text: str, email: str, source_url: str, *, field: str = "email"
) -> EvidenceMatch | None:
    """Match an exact parsed email token, never a prefix of a longer address."""
    wanted = email.strip().lower()
    if not wanted or EMAIL_RE.fullmatch(email.strip()) is None:
        return None
    for candidate in EMAIL_RE.finditer(text):
        if candidate.group(0).lower() == wanted:
            excerpt = _line_excerpt(text, candidate.start(), candidate.end())
            return recorded_match(field, email.strip(), source_url, excerpt)
    return None


def person_name_near_email(
    text: str, name: str, email_match: EvidenceMatch
) -> EvidenceMatch | None:
    """Require ordered name tokens in the same bounded email evidence fragment."""
    wanted = _tokens(name)
    if len(wanted) < 2:
        return None
    span = _token_span(email_match.excerpt, wanted)
    if span is None:
        return None
    return recorded_match(
        "name", name.strip(), email_match.source_url, email_match.excerpt
    )


def phrase(
    text: str, value: str, source_url: str, *, field: str
) -> EvidenceMatch | None:
    """Match a contiguous whole-token phrase such as a title, street, city, or state."""
    span = _token_span(text, _tokens(value))
    if span is None:
        return None
    start, end = span
    return recorded_match(
        field, value.strip(), source_url, _bounded_excerpt(text, start, end)
    )


def postal_code(text: str, value: str, source_url: str) -> EvidenceMatch | None:
    """Match one five- or nine-digit US postal code with digit boundaries."""
    wanted = value.strip()
    if re.fullmatch(r"\d{5}(?:-\d{4})?", wanted) is None:
        return None
    match = re.search(rf"(?<!\d){re.escape(wanted)}(?!\d)", text)
    if match is None:
        return None
    return recorded_match(
        "postal_code",
        wanted,
        source_url,
        _bounded_excerpt(text, match.start(), match.end()),
    )


def state(text: str, value: str, source_url: str) -> EvidenceMatch | None:
    """Match only a real USPS code or full state name with safe boundaries.

    Two-letter codes remain case-sensitive because ``OR``, ``IN``, and ``ME`` are
    ordinary words when case-folded. Full names are matched as whole token phrases.
    """
    wanted = value.strip()
    if not wanted:
        return None
    code = wanted.upper() if wanted.upper() in US_STATE_NAMES else ""
    if code:
        try:
            normalized = normalize_state_code(code)
        except ValueError:
            return None
        match = re.search(rf"(?<![A-Za-z]){re.escape(normalized)}(?![A-Za-z])", text)
        if match is None:
            return None
        return recorded_match(
            "state",
            wanted,
            source_url,
            _bounded_excerpt(text, match.start(), match.end()),
        )
    names = {name.lower(): name for name in US_STATE_NAMES.values()}
    canonical = names.get(wanted.lower())
    if canonical is None:
        return None
    span = _token_span(text, _tokens(canonical))
    if span is None:
        return None
    start, end = span
    return recorded_match(
        "state", wanted, source_url, _bounded_excerpt(text, start, end)
    )


def address_block(
    text: str, values: dict[str, str], source_url: str
) -> dict[str, EvidenceMatch]:
    """Prove address components inside one bounded block anchored by the street.

    A page often contains several addresses (district office, school, vendor footer).
    Matching street, city, state, and ZIP independently across the whole page can
    fabricate a composite address that never appeared. The street is therefore the
    anchor; only components found within its 500-character local block are returned,
    and every returned field carries that same excerpt.
    """
    street = str(values.get("street") or "").strip()
    street_span = _token_span(text, _tokens(street))
    if not street or street_span is None:
        return {}
    start, end = street_span
    # A local window alone is not an address boundary: two office/remittance
    # addresses can be less than 100 characters apart. Start with the containing
    # rendered line, then terminate at any other street-shaped address on that line.
    # This preserves a complete single-line address while preventing its state/ZIP
    # from being borrowed by the neighboring street.
    line_left = text.rfind("\n", 0, start) + 1
    line_right = text.find("\n", end)
    if line_right < 0:
        line_right = len(text)
    left = max(line_left, start - 220)
    right = min(line_right, end + 280)
    for candidate in ADDRESS_ANCHOR_RE.finditer(text, line_left, line_right):
        if candidate.end() <= start:
            left = max(left, candidate.end())
        elif candidate.start() >= end:
            right = min(right, candidate.start())
            break
    block = text[left:right]
    # State and ZIP must remain in the same sentence/clause as the extracted city.
    # A second address often uses a form the street-anchor vocabulary has never seen;
    # the full stop after the first city is still a reliable structural boundary.
    city_value = str(values.get("city") or "").strip()
    city_span = _token_span(block, _tokens(city_value))
    if city_span is not None:
        city_end = city_span[1]
        clause_end = re.search(r"(?:[;|•]|\.(?=\s|$))", block[city_end:])
        if clause_end is not None:
            block = block[: city_end + clause_end.start()]
    excerpt = " ".join(block.split())[:500]
    verified: dict[str, EvidenceMatch] = {}
    checks: tuple[tuple[str, EvidenceMatch | None], ...] = (
        ("street", phrase(block, street, source_url, field="street")),
        (
            "city",
            phrase(
                block,
                str(values.get("city") or "").strip(),
                source_url,
                field="city",
            ),
        ),
        (
            "state",
            state(block, str(values.get("state") or "").strip(), source_url),
        ),
        (
            "postal_code",
            postal_code(
                block,
                str(values.get("postal_code") or "").strip(),
                source_url,
            ),
        ),
    )
    for field_name, match in checks:
        if match is not None:
            verified[field_name] = recorded_match(
                field_name, match.value, source_url, excerpt
            )
    return verified


def _phone_digits(value: str) -> str:
    """Normalize one phone candidate while treating leading US country code equally."""
    digits = re.sub(r"\D", "", value)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def phone(text: str, value: str, source_url: str) -> EvidenceMatch | None:
    """Match one phone-like span; unrelated page numbers can never combine."""
    wanted = _phone_digits(value)
    if len(wanted) not in {7, 10}:
        return None
    candidates = [*PHONE_RE.finditer(text), *LOCAL_PHONE_RE.finditer(text)]
    for candidate in sorted(candidates, key=lambda item: item.start()):
        if _phone_digits(candidate.group(0)) == wanted:
            return recorded_match(
                "phone",
                value.strip(),
                source_url,
                _bounded_excerpt(text, candidate.start(), candidate.end()),
            )
    return None
