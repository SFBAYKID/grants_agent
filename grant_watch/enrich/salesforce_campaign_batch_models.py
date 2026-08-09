"""Typed inputs and results for multi-Campaign Salesforce batch preparation."""

from __future__ import annotations

from dataclasses import dataclass

from .salesforce_campaign_models import PreparedAction


@dataclass(frozen=True)
class CampaignTargetRequest:
    """One exact Campaign and Grant state/tier selection requested by a human."""

    campaign_link: str
    state: str
    grades: tuple[str, ...]
    # Which 200-organization slice of this selection to prepare, zero-based.
    # Salesforce accepts at most 200 records per collection call, and the selection
    # for a whole state and tier is routinely larger — 347 for California silver,
    # the request that dead-ended an SDR because the only advice on offer was
    # "refine", which the tool's own filters (state + tier) cannot express. Slices
    # are cut from ONE ordered selection so an organization lands in exactly one.
    slice_index: int = 0
    # The organization count the FIRST batch reported. Slices are cut by position
    # from a selection recomputed on every call, so if a lead leaves the set in
    # between — marked dead, regraded — every later organization shifts down one
    # place and the one on the boundary is silently never added. Passing the number
    # back makes that drift an explicit refusal instead of a silent gap. Zero means
    # "first batch, nothing to check against".
    expected_total_organizations: int = 0


@dataclass(frozen=True)
class PreparedCampaignBatch:
    """Durable batch result, optionally containing executable child actions."""

    batch_id: str
    summary: str
    actions: tuple[PreparedAction, ...] = ()
    state: str = "blocked_resolution"
