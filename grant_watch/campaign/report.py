"""PII-free deterministic shadow-readiness reporting for rich award cards."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from .policy import Reason
from .preparation import CandidateReview


@dataclass(frozen=True)
class ShadowReport:
    """Aggregate readiness only; no email, CRM id/link, or contact detail."""

    candidate_count: int
    ready_card_count: int
    rejection_counts: tuple[tuple[str, int], ...]
    ready_lead_ids: tuple[int, ...]


def build(reviews: tuple[CandidateReview, ...]) -> ShadowReport:
    """Aggregate deterministic readiness/rejection counts without sensitive facts."""
    rejected = Counter(
        item.reason.value for item in reviews if item.reason is not Reason.ELIGIBLE
    )
    ready = tuple(item.lead_id for item in reviews if item.draft is not None)
    return ShadowReport(
        candidate_count=len(reviews),
        ready_card_count=len(ready),
        rejection_counts=tuple(sorted(rejected.items())),
        ready_lead_ids=ready,
    )


def to_json(report: ShadowReport) -> str:
    """Serialize stable review output suitable for diffing across shadow days."""
    return json.dumps(
        {
            "candidate_count": report.candidate_count,
            "ready_card_count": report.ready_card_count,
            "ready_lead_ids": list(report.ready_lead_ids),
            "rejection_counts": dict(report.rejection_counts),
        },
        sort_keys=True,
        indent=2,
    )
