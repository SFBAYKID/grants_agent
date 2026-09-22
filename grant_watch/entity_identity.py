"""Human-confirmed organization identities for Grant rows that share a name.

WHY THIS EXISTS. A Campaign batch groups Grant rows into organizations. Rows with
an NCES id group by it; rows without one (every NSGP subaward, which USAspending
publishes with no recipient UEI, city or address) can only group by name+state,
and a group of more than one such row is treated as an identity COLLISION and left
out — two different "Central Christian School"s in one state must never collapse
into one Salesforce Lead. On 2026-09-22 that rule excluded 26 of 79 Oregon
organizations from a rep's campaign, and 25 of them were plainly one organization
with several subawards (same prime award, or the same grantee winning again in a
later cycle).

Nothing stored can PROVE those are single organizations, so the code rule stays.
This file records the cases a PERSON has confirmed, with who and when, in
`config/entity_identity_confirmations.json`. A confirmation lifts the collision for
exactly that canonical key and nothing else. Unknown or malformed files fail
closed: no confirmations, so the collision rule applies as before.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIRMATIONS_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "entity_identity_confirmations.json"
)


def confirmed_single_organizations(path: Path | None = None) -> frozenset[str]:
    """Return canonical name|STATE keys a human confirmed are one organization.

    Each entry must name its key, who confirmed it and when; an entry missing any
    of the three is ignored rather than trusted, because an unattributed identity
    claim is exactly what the collision rule exists to refuse.
    """
    try:
        source = path or CONFIRMATIONS_PATH  # read at call time so tests can patch it
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    entries = payload.get("confirmations") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return frozenset()
    keys: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("canonical_entity_key") or "").strip()
        who = str(entry.get("confirmed_by") or "").strip()
        when = str(entry.get("confirmed_on") or "").strip()
        if key and "|" in key and who and when:
            keys.add(key)
    return frozenset(keys)
