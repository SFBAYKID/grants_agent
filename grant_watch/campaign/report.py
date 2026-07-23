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
    draft_ready_count: int
    research_needed_count: int
    rejection_counts: tuple[tuple[str, int], ...]
    ready_lead_ids: tuple[int, ...]
    readiness_counts: tuple[tuple[str, int], ...]


def build(reviews: tuple[CandidateReview, ...]) -> ShadowReport:
    """Aggregate deterministic readiness/rejection counts without sensitive facts."""
    rejected = Counter(
        item.reason.value for item in reviews if item.reason is not Reason.ELIGIBLE
    )
    ready = tuple(item.lead_id for item in reviews if item.draft is not None)
    draft_ready = sum(
        1
        for item in reviews
        if item.draft is not None and item.draft.card_mode == "draft_ready"
    )
    research_needed = sum(
        1
        for item in reviews
        if item.draft is not None and item.draft.card_mode == "research_needed"
    )
    dimensions = {
        "contact_ready": sum(item.readiness.contact for item in reviews),
        "salesforce_ready": sum(item.readiness.salesforce for item in reviews),
        "organization_kind_ready": sum(
            item.readiness.organization_kind for item in reviews
        ),
        "source_run_link_ready": sum(
            item.readiness.source_run_link for item in reviews
        ),
        "mapped_route_ready": sum(item.readiness.mapped_route for item in reviews),
        "unassigned_route_ready": sum(
            item.readiness.unassigned_route for item in reviews
        ),
    }
    return ShadowReport(
        candidate_count=len(reviews),
        ready_card_count=len(ready),
        draft_ready_count=draft_ready,
        research_needed_count=research_needed,
        rejection_counts=tuple(sorted(rejected.items())),
        ready_lead_ids=ready,
        readiness_counts=tuple(sorted(dimensions.items())),
    )


def to_json(report: ShadowReport) -> str:
    """Serialize stable review output suitable for diffing across shadow days."""
    return json.dumps(
        {
            "candidate_count": report.candidate_count,
            "ready_card_count": report.ready_card_count,
            "draft_ready_count": report.draft_ready_count,
            "research_needed_count": report.research_needed_count,
            "ready_lead_ids": list(report.ready_lead_ids),
            "rejection_counts": dict(report.rejection_counts),
            "readiness_counts": dict(report.readiness_counts),
        },
        sort_keys=True,
        indent=2,
    )
