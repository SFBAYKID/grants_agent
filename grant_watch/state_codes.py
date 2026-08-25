"""Canonical United States postal-code vocabulary shared by runtime validators.

Shape-only validation accepted fictional codes such as ``ZZ`` and sent them into
paid or rate-limited source queries.  Keeping one reviewed mapping also gives
evidence binders the exact full state name without substring guesses.
"""

from __future__ import annotations


US_STATE_NAMES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

US_STATE_CODES: frozenset[str] = frozenset(US_STATE_NAMES)


def normalize_state_code(value: object) -> str:
    """Return a valid two-letter state/DC code or raise a clear error."""
    code = str(value or "").strip().upper()
    if code not in US_STATE_CODES:
        raise ValueError(f"unsupported US state/DC code {code or '(blank)'}")
    return code


def state_name(code: object) -> str:
    """Return the full name for a valid code, or an empty string when invalid."""
    return US_STATE_NAMES.get(str(code or "").strip().upper(), "")


# Reverse map for hand-entered data. Built from the same reviewed mapping above so
# the two directions cannot drift apart.
_NAMES_TO_CODES: dict[str, str] = {
    name.upper(): code for code, name in US_STATE_NAMES.items()
}


def state_code_or_blank(value: object) -> str:
    """Map a code OR a full state name to its code; "" when unrecognized.

    `normalize_state_code` RAISES on anything that is not already a code, which is
    right for our own validated inputs and wrong for data a human typed into a CRM.
    Salesforce holds the SAME state as "IL" on one record and "Illinois" on another
    -- measured in production 2026-08-25 across six Leads for one district -- and a
    caller comparing those two raw strings concludes the records are in different
    states. Returning "" for anything unrecognized lets a caller distinguish "no
    state on file" from "a state I could not parse" and fail safe on its own terms.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text in US_STATE_CODES:
        return text
    return _NAMES_TO_CODES.get(text, "")
