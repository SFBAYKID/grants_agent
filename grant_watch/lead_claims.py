"""Reps claiming leads: "I'm taking Gobles Public Schools", recorded with the receipt.

WHAT A CLAIM MEANS (Chase, 2026-09-01). It parks the lead and names the owner: Grant
keeps it out of the daily cards, stops chasing anybody about it, and tells another rep
who asks that it is taken. It holds until a human releases it — there is no expiry and
no automatic reopening, because a lead quietly returning to the pool is Grant posting
the same thing a rep already told it they were handling.

A CLAIM IS NOT A SALESFORCE CHANGE, and no wording built on this may imply it is.
Grant's Salesforce client is create-only; it cannot set an Owner on anything. This
ledger is Grant's own record of who said what in Slack, and the caller's reply has to
say so — the rep who triggered this feature had already been told, correctly, that
ownership is between him and Salesforce.

RESOLUTION IS BY CANONICAL KEY, AND THAT IS THE WHOLE SAFETY STORY. A rep names an
organization, not a lead id, and one organization can hold several lead rows: the
laptop copy holds two `GOBLES PUBLIC SCHOOLS` MI rows, and Salesforce showed two
Michigan records plus one in MAINE. So a name is grouped with
`db.canonical_entity_key`, which normalizes punctuation and case and carries the STATE
— Gobles MI and Gobles ME can never collapse into one another. One key means one
organization and every row of it is claimed together; more than one key means Grant
does not know which was meant and must ASK. It never picks the closest.

NEVER FALL BACK TO THE RAW NAME when the stored key is empty. `org_backfill` records
what that costs: a `COALESCE(canonical_entity_key, entity_name)` fallback produced a
value that can never equal a stored canonical key, which split one organization in two
and re-surfaced leads that had been cited as fixed. The key is recomputed here with the
same function that wrote it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import SEARCHABLE_LEAD_PREDICATE, canonical_entity_key


@dataclass(frozen=True)
class Organization:
    """Every lead row Grant holds for one organization, addressed as one thing."""

    key: str
    entity_name: str
    state: str
    lead_ids: tuple[int, ...]


@dataclass(frozen=True)
class Claim:
    """One live claim, with the words and the coordinates that prove it."""

    lead_id: int
    slack_user: str
    audience: str
    thread_ts: str
    message_ts: str
    claim_text: str
    claimed_at: str


class AlreadyClaimed(Exception):
    """Somebody else holds a live claim on part of this organization.

    Carries the winning claim so the caller can name the holder and the date instead
    of reporting a bare failure — and so it can never silently transfer ownership.
    """

    def __init__(self, held_by: Claim) -> None:
        """Carry the winning claim so the caller can name who actually holds it."""
        super().__init__(f"already claimed by {held_by.slack_user}")
        self.held_by = held_by


def _utc(now: datetime | None) -> datetime:
    """The caller's clock, or the real one read at THIS moment.

    Every entry point takes `now` and every one of them routes through here. The
    poll-lease defect of 2026-08-26 was exactly this asymmetry: `acquire` accepted an
    injected clock and `release` read the wall clock, so the two disagreed under test
    and nowhere else.
    """
    return now or datetime.now(timezone.utc)


def _like_literal(raw: str) -> str:
    """Escape SQLite LIKE metacharacters so a rep's text stays a literal substring."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _key_for(entity_name: str, state: str, stored: str) -> str:
    """The organization key for one lead row, recomputed when the column is empty."""
    return stored.strip() or canonical_entity_key(entity_name, state)


def resolve(conn: sqlite3.Connection, name: str, state: str = "") -> list[Organization]:
    """Every organization whose name contains `name`, grouped so duplicates are one.

    Returns a list because AMBIGUITY IS THE CALLER'S PROBLEM TO SURFACE, not this
    function's to guess away: two entries mean Grant must ask which was meant. Sorted
    by name then state so the question a rep is asked is stable between attempts.
    """
    wanted = name.strip()
    if not wanted:
        return []
    sql = (
        "SELECT id, entity_name, state, "
        "COALESCE(canonical_entity_key,'') AS stored_key FROM leads "
        f"WHERE {SEARCHABLE_LEAD_PREDICATE} "
        "AND UPPER(entity_name) LIKE ? ESCAPE '\\'"
    )
    params: list[object] = [f"%{_like_literal(wanted).upper()}%"]
    if state.strip():
        sql += " AND UPPER(state)=?"
        params.append(state.strip().upper())
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(sql, params):
        key = _key_for(
            str(row["entity_name"] or ""),
            str(row["state"] or ""),
            str(row["stored_key"] or ""),
        )
        grouped.setdefault(key, []).append(row)
    organizations = [
        Organization(
            key=key,
            # The longest name of the group: a truncated or abbreviated duplicate is
            # never the one a human should be shown when a fuller one exists.
            entity_name=max(
                (str(row["entity_name"] or "") for row in rows), key=len, default=""
            ),
            state=str(rows[0]["state"] or ""),
            lead_ids=tuple(sorted(int(row["id"]) for row in rows)),
        )
        for key, rows in grouped.items()
    ]
    return sorted(organizations, key=lambda org: (org.entity_name, org.state))


def _row_to_claim(row: sqlite3.Row) -> Claim:
    """Build the typed claim from one ledger row."""
    return Claim(
        lead_id=int(row["lead_id"]),
        slack_user=str(row["slack_user"] or ""),
        audience=str(row["audience"] or ""),
        thread_ts=str(row["thread_ts"] or ""),
        message_ts=str(row["message_ts"] or ""),
        claim_text=str(row["claim_text"] or ""),
        claimed_at=str(row["claimed_at"] or ""),
    )


def live_claims(
    conn: sqlite3.Connection, lead_ids: tuple[int, ...] | list[int]
) -> dict[int, Claim]:
    """The live claim on each of these leads, keyed by lead id; absent means free."""
    wanted = [int(lead_id) for lead_id in lead_ids]
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"""SELECT lead_id,slack_user,audience,thread_ts,message_ts,claim_text,
                   claimed_at
              FROM lead_claims
             WHERE released_at IS NULL AND lead_id IN ({placeholders})""",
        wanted,
    ).fetchall()
    return {int(row["lead_id"]): _row_to_claim(row) for row in rows}


def claim(
    conn: sqlite3.Connection,
    organization: Organization,
    *,
    slack_user: str,
    audience: str,
    thread_ts: str,
    message_ts: str,
    claim_text: str,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Record `slack_user` as the holder of every lead row in `organization`.

    Returns (newly claimed, already held by this same person) so the caller can tell
    "done" from "you already had this" without a second query — and never reports a
    write that did not happen.

    ALL OR NOTHING. If any row of the organization is held by somebody else the whole
    call raises `AlreadyClaimed` and writes nothing: half an organization belonging to
    one rep and half to another is a state no honest message can describe. The partial
    unique index is what makes that safe under a race — two reps claiming in the same
    minute means the loser's INSERT raises, and the winner is re-read and reported
    rather than overwritten.
    """
    if not slack_user.strip():
        raise ValueError("a claim must name the person making it")
    if not claim_text.strip():
        raise ValueError("a claim must carry the words it came from")
    stamp = _utc(now).isoformat()
    held = live_claims(conn, organization.lead_ids)
    foreign = next(
        (item for item in held.values() if item.slack_user != slack_user), None
    )
    if foreign is not None:
        raise AlreadyClaimed(foreign)
    fresh = [lead_id for lead_id in organization.lead_ids if lead_id not in held]
    if fresh:
        try:
            with conn:
                conn.executemany(
                    """INSERT INTO lead_claims
                         (lead_id,slack_user,audience,thread_ts,message_ts,
                          claim_text,claimed_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    [
                        (
                            lead_id,
                            slack_user,
                            audience,
                            thread_ts,
                            message_ts,
                            claim_text,
                            stamp,
                        )
                        for lead_id in fresh
                    ],
                )
        except sqlite3.IntegrityError as exc:
            # Someone claimed a row between the read above and this write. Re-read so
            # the caller names the person who actually holds it, never a guess.
            winner = next(
                (
                    item
                    for item in live_claims(conn, organization.lead_ids).values()
                    if item.slack_user != slack_user
                ),
                None,
            )
            if winner is None:
                raise
            raise AlreadyClaimed(winner) from exc
    return len(fresh), len(held)


def release(
    conn: sqlite3.Connection,
    organization: Organization,
    *,
    released_by: str,
    note: str = "",
    now: datetime | None = None,
) -> int:
    """End every live claim on this organization; returns how many were ended.

    Reversibility is not the same question as expiry. Chase settled that a claim never
    expires on a timer — a lead must not drift back into the pool a rep was told it
    was out of. But a claim made from one misread sentence would otherwise remove a
    real lead from the product permanently, with no way back, so a human can always
    hand it back.
    """
    if not released_by.strip():
        raise ValueError("a release must name the person making it")
    stamp = _utc(now).isoformat()
    placeholders = ",".join("?" for _ in organization.lead_ids)
    if not placeholders:
        return 0
    with conn:
        cursor = conn.execute(
            f"""UPDATE lead_claims
                   SET released_at=?, released_by=?, release_note=?
                 WHERE released_at IS NULL AND lead_id IN ({placeholders})""",
            [stamp, released_by, note.strip() or None, *organization.lead_ids],
        )
    return int(cursor.rowcount)


def is_claimed(conn: sqlite3.Connection, lead_id: int) -> bool:
    """Whether one lead is held right now.

    Used where a row is checked on its own rather than filtered out of a set — the
    final cancellation veto in `campaign.delivery`, which is the last gate between a
    prepared card and the Slack call. A claim landing in THAT window would otherwise
    post the card anyway: `review_candidates` ran before the claim existed.
    """
    return (
        conn.execute(
            "SELECT 1 FROM lead_claims WHERE lead_id=? AND released_at IS NULL LIMIT 1",
            (int(lead_id),),
        ).fetchone()
        is not None
    )
