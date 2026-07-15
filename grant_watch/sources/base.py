"""Shared HTTP plumbing for source pollers.

Why: every poller talks to government servers, and CLAUDE.md requires polite behavior —
identify ourselves, sleep between requests, fail loudly (raise) rather than emit garbage.
Parsers are kept PURE (payload -> list[RawItem]) so tests run on recorded fixtures
without touching live servers (architectural.md §8).
"""

from __future__ import annotations

import time
from typing import Any  # requests accepts heterogeneous query/JSON scalar values.

import requests

USER_AGENT = "MonarchGrantWatch/0.2 (Monarch Connected; contact: chase@monarchconnected.com)"
REQUEST_TIMEOUT_S = 30
SLEEP_BETWEEN_REQUESTS_S = 1.0  # per-request politeness pause for gov servers


def polite_get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    """GET with our UA, a timeout, and a politeness sleep. Raises on HTTP errors."""
    time.sleep(SLEEP_BETWEEN_REQUESTS_S)
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S,
                        headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp


def polite_post(url: str, json_body: dict[str, Any]) -> requests.Response:
    """POST (JSON) with our UA, a timeout, and a politeness sleep. Raises on HTTP errors."""
    time.sleep(SLEEP_BETWEEN_REQUESTS_S)
    resp = requests.post(url, json=json_body, timeout=REQUEST_TIMEOUT_S,
                         headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp
