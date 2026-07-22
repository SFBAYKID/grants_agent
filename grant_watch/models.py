"""Typed data models for grant_watch.

Why: the Constitution (CLAUDE.md rule 2) bans untyped dict blobs flowing through the
pipeline. Pollers emit RawItem; scoring turns RawItem into a graded Lead; db.py persists
Leads. Keeping these as frozen-ish dataclasses makes every field explicit and testable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any  # Raw evidence preserves source-owned JSON without coercion.


class LeadGrade(str, Enum):
    """Chase's grading ladder. GOLD = entity just GOT security money (spend window open,
    ideally < 12 months old). SILVER = entity is applying / has an open RFP.
    WATCH = ambiguous or pipeline signal — kept rather than dropped (CLAUDE.md)."""

    GOLD = "gold"
    SILVER = "silver"
    WATCH = "watch"


class FundingEventType(str, Enum):
    """Evidence-backed event kinds Grant may describe to a human.

    ``RECORD_OBSERVED`` is deliberately internal-facing: it means Grant first saw a
    source row, not that the underlying award or application happened that day.
    """

    AWARD_ANNOUNCED = "award_announced"
    AWARD_OBLIGATED = "award_obligated"
    APPLICATION_SUBMITTED = "application_submitted"
    APPLICATION_WINDOW_OPENED = "application_window_opened"
    RFP_POSTED = "rfp_posted"
    FUNDING_CYCLE_CHANGED = "funding_cycle_changed"
    RECORD_OBSERVED = "record_observed"


class DatePrecision(str, Enum):
    """How precisely a source identifies an event occurrence date."""

    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    """Truth status attached to source evidence, never inferred from lead grade."""

    VERIFIED = "verified"
    ASSUMED = "assumed"
    NEEDS_TESTING = "needs-testing"


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
    # REQUIRED, keyword-only, and deliberately without a default (Chase, 2026-07-22).
    # The old `= RECORD_OBSERVED` default is what MANUFACTURED unknown records: a source
    # that forgot the field produced rows that assert nothing forever, and three test
    # fixtures silently built "awards" that were not awards — which is how a grade-driven
    # wording bug reached outbound email undetected. `record_semantics` treats an unknown
    # event as "claim nothing", so a missing event type is not a small omission; it
    # silently degrades every downstream surface. Forcing the decision at construction is
    # the one change that stops the class. kw_only because this field sits after
    # defaulted ones and must not be passed positionally.
    event_type: FundingEventType = field(kw_only=True)
    event_date: str = ""
    date_precision: DatePrecision = DatePrecision.UNKNOWN
    funded_scope: str = ""
    eligible_scope: str = ""
    application_portal: str = ""
    source_locator: str = ""
    evidence_excerpt: str = ""
    verification_status: VerificationStatus = VerificationStatus.NEEDS_TESTING
    backfill: bool = False

    def raw_json(self, cap: int = 5000) -> str:
        """Serialize valid JSON within ``cap`` without slicing through syntax.

        Oversized source payloads become a bounded audit envelope with a checksum and
        preview. The checksum preserves change evidence while the envelope remains
        parseable by every downstream reader.
        """
        if cap < 2:
            raise ValueError("raw JSON cap must allow at least an empty object")
        serialized = json.dumps(self.raw, default=str)
        if len(serialized) <= cap:
            return serialized
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        envelope: dict[str, object] = {
            "_truncated": True,
            "original_length": len(serialized),
            "sha256": digest,
            "preview": "",
        }
        minimal = json.dumps(envelope, separators=(",", ":"))
        if len(minimal) > cap:
            return "{}"
        envelope["preview"] = serialized[: cap - len(minimal)]
        bounded = json.dumps(envelope, separators=(",", ":"))
        while len(bounded) > cap and envelope["preview"]:
            excess = len(bounded) - cap
            envelope["preview"] = str(envelope["preview"])[:-excess]
            bounded = json.dumps(envelope, separators=(",", ":"))
        return bounded

    def observation_hash(self) -> str:
        """Return a stable hash of fields whose change can create a new event.

        Raw payloads often contain request metadata that changes on every fetch. The
        hash therefore covers only source facts Grant may persist or communicate.
        """
        facts = {
            "title": self.title,
            "entity": self.entity,
            "state": self.state,
            "program": self.program,
            "amount": self.amount,
            "start": self.start,
            "end": self.end,
            "url": self.url,
            "event_type": self.event_type.value,
            "event_date": self.event_date,
            "date_precision": self.date_precision.value,
            "funded_scope": self.funded_scope,
            "eligible_scope": self.eligible_scope,
            "application_portal": self.application_portal,
            "source_locator": self.source_locator,
            "evidence_excerpt": self.evidence_excerpt,
            "verification_status": self.verification_status.value,
        }
        payload = json.dumps(facts, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceObservation:
    """Immutable normalized snapshot of one source item version."""

    source: str
    source_item_id: str
    payload_hash: str
    raw_json: str
    source_url: str
    source_locator: str
    verification_status: VerificationStatus


@dataclass(frozen=True)
class FundingEvent:
    """One source-supported funding event safe for ranking and wording."""

    event_type: FundingEventType
    occurred_on: str
    date_precision: DatePrecision
    amount: float | None
    funded_scope: str
    eligible_scope: str
    application_portal: str
    evidence_excerpt: str
    source_url: str
    source_locator: str
    verification_status: VerificationStatus
    backfill: bool = False


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
    complete: bool = True
    error_code: str = ""
