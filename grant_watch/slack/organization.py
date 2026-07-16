"""Verified official-site organization details for lead-thread location questions."""

from __future__ import annotations

from collections.abc import Callable

import requests

from ..enrich import finder
from ..enrich.organization_profile import fetch_profile
from ..presentation import display_entity_name

Progress = Callable[[str], None]


def find_organization_details(
        entity: str, state: str,
        on_progress: Progress | None = None) -> str:
    """Find and code-verify official address, phone, and website details."""
    site = finder.find_official_site(entity, state, on_progress=on_progress)
    clean_entity = display_entity_name(entity)
    if site is None:
        return (
            f"I couldn’t verify an official website or street address for {clean_entity}. "
            f"The funding record only identifies the state as {state or 'not published'}, "
            "so I won’t guess at a location."
        )
    try:
        profile = fetch_profile(entity, site.domain, site.url)
    except (KeyError, ValueError, RuntimeError, requests.RequestException):
        return (
            f"I found the official website for {clean_entity}, but I couldn’t verify a "
            f"street address from the page I could read.\n\n"
            f"• *State:* {state or 'not published'}\n"
            f"• *Official website:* <https://{site.domain}/|{site.domain}>\n"
            f"• *Source checked:* <{site.url}|official website page>"
        )
    address = ", ".join(filter(None, (
        profile.street, profile.city, profile.state or state, profile.postal_code)))
    lines = [
        f"• *Address:* {address or 'not published on the verified page'}",
        f"• *Official website:* <{profile.website}|{site.domain}>",
        f"• *Source checked:* <{profile.source_url or site.url}|official website page>",
    ]
    if profile.main_phone:
        lines.insert(1, f"• *Main phone:* {profile.main_phone}")
    return f"Here’s the verified location for *{clean_entity}*:\n\n" + "\n".join(lines)
