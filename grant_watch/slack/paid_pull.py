"""The money gate for bulk contact purchase: price it first, then spend.

Split out of `tools` at the 1000-line cap (rule 4). One responsibility, and it is
the one that spends real money: how much a single confirmed pull may cost, and the
proof that a human was shown that cost before it was spent.
"""

from __future__ import annotations

from ..presentation import model_note

# The most a single bulk pull may ever spend, regardless of what the model asks for.
# 40 covered the 13-lead California campaign that motivated the bulk path. Raised to
# 100 on Chase's instruction, 2026-08-11, verbatim: "you still need to ensure that
# the rep can add up to 100 leads and enrich up to 100 leads every single time."
#
# RAISING IT REQUIRED MAKING THE TWO-STEP REAL FIRST. The comment that shipped with
# this change said the priced run was "the real protection" — while the code eight
# lines below already said the opposite, correctly: the price-then-confirm protocol
# lived only in the tool DESCRIPTION, so a model could send `confirm=true` on its
# very first call and spend without anyone seeing a number. `_require_priced_run`
# now enforces it server-side, which is what makes 100 defensible rather than just
# 2.5x the un-approved blast radius.
MAX_CREDITS_PER_CALL = 100

# How long a free priced run authorises the confirmed pull that follows it. Long
# enough for a rep to read a number and answer, short enough that an old estimate
# cannot authorise a spend against a changed lead set.
PRICED_RUN_TTL_S = 3600.0
# requester + exact lead set -> when it was priced. Process-local on purpose: a
# restart forgets, and forgetting means the rep is asked to price again, which is
# the safe direction.
_PRICED_RUNS: dict[str, tuple[float, int]] = {}


def _priced_run_key(requester_slack: str, lead_ids: list[int]) -> str:
    """Identify one exact (rep, lead set) pricing, so a different set re-prices."""
    return f"{requester_slack}:" + ",".join(str(item) for item in sorted(set(lead_ids)))


def _record_priced_run(
    requester_slack: str, lead_ids: list[int], max_credits: int
) -> None:
    """Remember the exact lead set AND ceiling that were priced, free, for this rep."""
    import time

    _PRICED_RUNS[_priced_run_key(requester_slack, lead_ids)] = (
        time.monotonic(),
        max_credits,
    )
    # Bounded: a long-lived listener would otherwise accumulate a key per priced run
    # forever. Expired entries are dropped whenever a new one is recorded.
    cutoff = time.monotonic() - PRICED_RUN_TTL_S
    for key in [k for k, (at, _) in _PRICED_RUNS.items() if at < cutoff]:
        del _PRICED_RUNS[key]


def _require_priced_run(
    requester_slack: str, lead_ids: list[int], max_credits: int
) -> bool:
    """Whether this exact spend was priced recently enough to be confirmed.

    THE CEILING IS PART OF THE BILL. Keyed on the lead set alone, a rep could be
    shown "would spend 5 credits" and the confirm could then pass max_credits=100 —
    measured, and it spent 100. The number the rep saw has to be the number that
    binds, so a confirm above the priced ceiling is refused.
    """
    import time

    entry = _PRICED_RUNS.get(_priced_run_key(requester_slack, lead_ids))
    if entry is None:
        return False
    priced_at, priced_ceiling = entry
    return (
        time.monotonic() - priced_at
    ) <= PRICED_RUN_TTL_S and max_credits <= priced_ceiling


def _zoominfo_fill_many(
    lead_ids: list[int], max_credits: int, confirm: bool, requester_slack: str
) -> str:
    """Price, or buy, decision-maker contacts across several leads at once.

    THE GAP THIS CLOSES. A rep asked "Do it for all" and there was no way to say yes:
    every contact had to be bought one lead at a time through its own approval
    conversation, so 997 of 1000 purchased credits sat unused beside 62 contacts with
    no email, phone or mobile at all. The engine already existed as a CLI command;
    reps do not have a terminal.

    `confirm=false` runs only FREE searches and reports the exact bill, which is what
    makes the approval real rather than a formality — the rep sees the number before
    anyone spends it.
    """
    from .. import contact_fill, db

    if not lead_ids:
        return "ERROR: tell me which leads to fill."
    if max_credits <= 0:
        return "ERROR: I need a credit ceiling above zero before I can price this."
    # THE MODEL SUPPLIES `max_credits`, SO IT CANNOT BE THE ONLY CEILING. The
    # two-step "price it, then confirm" protocol lived in the tool DESCRIPTION,
    # which is a prompt instruction — and the rule here is that the safety is the
    # shape, not the prompt. A model may call confirm=true on the first turn, and
    # several tool_use blocks across six turns compound it.
    if max_credits > MAX_CREDITS_PER_CALL:
        return (
            f"ERROR: I cap a single bulk pull at {MAX_CREDITS_PER_CALL} credits. "
            f"Ask for {MAX_CREDITS_PER_CALL} or fewer, or split the leads."
        )
    # SO THE PROTOCOL IS NOW ENFORCED HERE, not described. `confirm=true` is only
    # honoured for a lead set this rep has already had priced for free, which is
    # what makes the number above a bill somebody actually saw. Before this, the
    # first call could spend the ceiling with no estimate shown to anyone.
    if confirm and not _require_priced_run(requester_slack, lead_ids, max_credits):
        return (
            "ERROR: nothing was spent. I price a bulk pull for free first so you "
            "can see the exact cost, and this exact set of leads has not been "
            f"priced in the last {int(PRICED_RUN_TTL_S // 60)} minutes. Call me "
            "again with confirm=false, show the rep the number, and confirm only "
            "after they say yes."
        )

    conn = db.connect()
    remaining = contact_fill.remaining_credits(conn)
    if max_credits > remaining:
        return (
            f"ERROR: that ceiling is {max_credits} credits but only {remaining} "
            "remain this period. Lower it and I'll price the run."
        )
    outcome = contact_fill.fill_contacts(
        conn,
        lead_ids,
        max_credits=max_credits,
        dry_run=not confirm,
        requested_by=requester_slack,
    )
    if not confirm:
        _record_priced_run(requester_slack, lead_ids, max_credits)
        return (
            f"PRICED, NOTHING SPENT: {outcome.summary()}. "
            f"{remaining} credits remain. "
            f"{model_note('Show the rep this exact cost and ask for a yes before calling again with confirm=true.')}"
        )
    return (
        f"BOUGHT: {outcome.summary()}. "
        f"{contact_fill.remaining_credits(conn)} credits remain."
    )
