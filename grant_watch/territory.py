"""Territory ownership: which Monarch rep owns the state a lead sits in.

Why (Chase, 2026-07-22): proactive cards went out addressed to nobody, so nobody felt
responsible for one and nobody replied. Naming the rep who owns that state turns a
broadcast into an assignment — and an `<@Uxxxx>` mention is a real Slack notification
on that person's phone, which a channel post never is.

Honesty rules apply here exactly as they do to lead data (Constitution rule 1):
  * A Slack user id is NEVER guessed or inferred from a name. An unmapped state
    produces NO mention rather than a plausible-looking wrong one — tagging the wrong
    rep is worse than tagging nobody, because it silently reassigns real revenue.
  * A malformed id from configuration is rejected and reported on stderr (which the
    cron captures) rather than rendered into a post as broken text.

The map is data, not code: `GRANT_TERRITORY_OWNERS` overrides it without a deploy, so
new states and reps are added by config (CLAUDE.md: "expand to more states by config,
not code"). Format: "PA=U08C1NBH875,CA=U01DFJWQQJ3,WA=U01E908206M".
"""

from __future__ import annotations

import os
import re
import sys

from .presentation import state_display_name

# Slack ids are `U`/`W` followed by uppercase alphanumerics. Validated rather than
# trusted so a typo in .env can never emit `<@garbage>` — or anything injectable —
# into a channel post.
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,20}$")

# Verified 2026-07-22 against the Monarch Slack directory (users.list via the
# workspace search): every id below belongs to the named @monarchconnected.com
# account. Do not edit an id here without re-reading it from Slack.
DEFAULT_TERRITORY_OWNERS: dict[str, str] = {
    "PA": "U08C1NBH875",  # Brett D'Ambrosio  <brett@monarchconnected.com>
    "CA": "U01DFJWQQJ3",  # Anthony Dambrosio  <anthony@monarchconnected.com>
    "WA": "U01E908206M",  # Kerry Hilligus  <kerry@monarchconnected.com>
    "TX": "U01E908206M",  # Kerry Hilligus
    "OR": "U01E908206M",  # Kerry Hilligus
}


def _parse_override(raw: str) -> dict[str, str]:
    """Parse GRANT_TERRITORY_OWNERS, dropping (and reporting) unusable entries.

    Skips rather than raises: a bad env value must not crash every drip tick, and a
    dropped entry fails safe — that state simply goes untagged.
    """
    owners: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        state, sep, user = pair.partition("=")
        state, user = state.strip().upper(), user.strip()
        if not sep or not re.fullmatch(r"[A-Z]{2}", state):
            print(
                f"[territory] ignoring malformed entry {pair!r} "
                "(expected STATE=SLACK_USER_ID)",
                file=sys.stderr,
            )
            continue
        if not _SLACK_ID_RE.match(user):
            print(
                f"[territory] ignoring {state}: {user!r} is not a Slack user id",
                file=sys.stderr,
            )
            continue
        owners[state] = user
    return owners


def territory_owners() -> dict[str, str]:
    """Return the active state -> Slack user id map.

    A configured `GRANT_TERRITORY_OWNERS` REPLACES the defaults outright (rather than
    merging) so the environment is always the complete, auditable picture of who is
    being tagged — a half-overridden map is the kind of thing nobody notices until a
    rep is tagged on another rep's deal.

    Presence of the variable, not the parsed result, decides: if it is set but every
    entry is malformed, the answer is "nobody is mapped", NOT "fall back to the
    built-in reps". Falling back would quietly tag people the operator was actively
    trying to change — failing toward no tag is the safe direction.
    """
    raw = os.environ.get("GRANT_TERRITORY_OWNERS", "")
    if raw.strip():
        return _parse_override(raw)
    return dict(DEFAULT_TERRITORY_OWNERS)


def owner_for_state(state: object) -> str | None:
    """Return the Slack user id owning `state`, or None when nobody is mapped."""
    code = str(state or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        return None
    return territory_owners().get(code)


# Only sources whose state is a FACT may tag a human. For these, `state` is either the
# API query filter the rows were requested under (usaspending) or a constant the poller
# hardcodes because the source covers exactly one state (WEBS=WA, OregonBuys=OR,
# SAM=WA place-of-performance, CA portal=CA). Anything not listed here is treated as
# inferred and posts untagged.
#
# The excluded case is real and live: `rfp_aggregator._row_state` derives a state by
# searching the row's prose for five state NAMES, so "Oregon City Schools, Ohio" reads
# as OR, "City of California, Missouri" as CA, and "1600 Pennsylvania Avenue NW" as PA.
# Before territory tagging that produced a wrong two-letter label on a card; now it
# would send a rep's phone a notification asserting they own someone else's deal.
# An allowlist (not a blocklist) so a NEW source is untagged until proven, never
# silently trusted.
# Namespaced sources: the part after the colon varies (a CFDA number, a fiscal year), so
# these must match by PREFIX. `usaspending:` derives state from the API query filter;
# `ca-grants-award:` hardcodes "CA".
VERIFIED_STATE_SOURCE_PREFIXES: tuple[str, ...] = (
    "usaspending:",
    "usaspending-subaward:",  # `assumed`: the recipient_locations filter is believed to
    # bind the sub-recipient under subawards=true; not evidenced in code or a cited doc.
    "ca-grants-award:",
)
# Constant-state sources: the whole source name is fixed, so these must match EXACTLY.
# Prefix-matching them would trust a future `webs-national` or `sam.gov-scraped` purely
# because of how it was named — the failure this allowlist exists to prevent.
VERIFIED_STATE_SOURCE_NAMES: frozenset[str] = frozenset(
    {
        "ca-grants-portal",  # hardcodes "CA"
        "webs",  # hardcodes "WA"
        "oregonbuys",  # hardcodes "OR"
        "sam.gov",  # hardcodes "WA"; `assumed` to mean place-of-performance
    }
)


def state_is_verified(source: object) -> bool:
    """Whether this source's `state` is evidence rather than inference.

    NOTE none of the four constant-state names can currently produce a proactive card —
    `rfp_candidates` hardcodes `source='rfp'`, `nugget_candidates` requires an award
    event, and `bulletin_candidates` hardcodes grants.gov/ca-grants-portal. They are
    listed so the classification stays complete as those queries change, not because
    they fire today.
    """
    name = str(source or "")
    return name in VERIFIED_STATE_SOURCE_NAMES or name.startswith(
        VERIFIED_STATE_SOURCE_PREFIXES
    )


def mention_line(state: object, source: object = None) -> str:
    """Return the '\\n\\n<@U…> — <State> is your territory…' line for a proactive card.

    Empty string when the state is unknown, unowned, or came from a source that only
    INFERRED it (see VERIFIED_STATE_SOURCES) — the card then goes out untagged. The
    rendered text contains no source-controlled input: the id comes from the validated
    map and the state name from a fixed lookup, so nothing injectable can reach the
    mention (Slack renders this post with mrkdwn on).

    `source` defaults to None, which is UNVERIFIED. A caller that forgets to pass it
    gets no mention rather than a possibly-wrong one.
    """
    if not state_is_verified(source):
        return ""
    owner = owner_for_state(state)
    if not owner:
        return ""
    name = state_display_name(state)
    where = f"{name} is your territory" if name else "this one is your territory"
    return f"\n\n<@{owner}> — {where}. Want me to find the right contact?"
