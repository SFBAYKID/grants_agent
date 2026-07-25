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


@dataclass(frozen=True)
class PreparedCampaignBatch:
    """Durable batch result, optionally containing executable child actions."""

    batch_id: str
    summary: str
    actions: tuple[PreparedAction, ...] = ()
    state: str = "blocked_resolution"
