"""Typed data models for grant_watch.

Why: the Constitution (CLAUDE.md rule 2) bans untyped dict blobs flowing through the
pipeline. Pollers emit RawItem; scoring turns RawItem into a graded Lead; db.py persists
Leads. Keeping these as frozen-ish dataclasses makes every field explicit and testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LeadGrade(str, Enum):
    """Chase's grading ladder. GOLD = entity just GOT security money (spend window open,
    ideally < 12 months old). SILVER = entity is applying / has an open RFP.
    WATCH = ambiguous or pipeline signal — kept rather than dropped (CLAUDE.md)."""

    GOLD = "gold"
    SILVER = "silver"
    WATCH = "watch"


@dataclass
class RawItem:
    """One record as returned by a source poller, before grading.

    source        stable source key INCLUDING sub-source, e.g. 'usaspending:16.071'.
                  The CFDA suffix is load-bearing: SVPP spans two CFDA codes and the
                  dedup key is (source, item_id) — see docs/FINDINGS.md.
    item_id       source-native unique id (award id, opportunity id, notice id).
    title         human-readable one-liner (award description / opportunity title).
    entity        recipient / posting agency name ('' when the source hides it).
    state         two-letter state when known, else ''.
    program       program tag (SVPP, NSGP, CHP, RFP:webs, ...) when derivable, else ''.
    amount        award/oppty dollars; None when the source has none; negatives are
                  real (de-obligations) and are graded down by scoring.py.
    start / end   ISO dates of the spend window (or close date for RFPs); '' if unknown.
    url           deep link for the human in Slack.
    raw           trimmed source payload for audit (stored as JSON, capped by db.py).
    """

    source: str
    item_id: str
    title: str
    entity: str
    state: str
    program: str
    amount: float | None
    start: str
    end: str
    url: str
    raw: dict[str, Any] = field(default_factory=dict)

    def raw_json(self, cap: int = 5000) -> str:
        """Serialize the raw payload for storage, capped so the DB stays lean."""
        return json.dumps(self.raw, default=str)[:cap]


@dataclass
class Lead:
    """A graded RawItem, ready for persistence. Mirrors the `leads` table (db.py)."""

    item: RawItem
    grade: LeadGrade
    entity_type: str = ""  # district, city, nonpublic_school, nonprofit, '' unknown


@dataclass
class RunStats:
    """Per-source stats for one poll run. Mirrors the `runs` table."""

    source: str
    items_seen: int = 0
    items_new: int = 0
    errors: str = ""
