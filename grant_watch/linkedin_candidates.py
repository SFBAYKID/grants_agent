"""Durable, thread-bound evidence for LinkedIn-only person candidates.

LinkedIn search-result identities are not verified-email contacts. This store keeps
that weaker evidence class explicit while allowing a user's phrase such as "this guy"
to resolve to exactly the person Grant just showed in the same Slack thread.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .enrich.finder import LinkedInPerson

_CANDIDATE_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class LinkedInCandidate:
    """One active LinkedIn identity bound to a Grant lead and Slack context."""

    candidate_id: str
    lead_id: int
    workspace: str
    channel: str
    thread_ts: str
    requested_by: str
    person_name: str
    title: str
    profile_url: str
    organization: str
    evidence_excerpt: str
    expires_at: str


def _now() -> datetime:
    """Return an aware UTC timestamp for candidate expiry decisions."""
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    """Serialize timestamps consistently for SQLite and immutable action payloads."""
    return value.isoformat(timespec="seconds")


def _evidence_hash(person: LinkedInPerson, organization: str) -> str:
    """Fingerprint the exact organization-bound search-result evidence."""
    raw = "\n".join((organization, person.name, person.title, person.url,
                     person.evidence_excerpt))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_candidate(
        conn: sqlite3.Connection, lead_id: int, workspace: str, channel: str,
        thread_ts: str, requested_by: str, organization: str,
        person: LinkedInPerson) -> LinkedInCandidate:
    """Replace the active candidate in one exact context with the shown person."""
    if not all((lead_id > 0, workspace, channel, thread_ts, requested_by,
                organization.strip(), person.name.strip(), person.url.strip(),
                person.evidence_excerpt.strip())):
        raise ValueError("LinkedIn candidate context and evidence are required")
    now = _now()
    candidate_id = str(uuid.uuid4())
    expires = now + _CANDIDATE_TTL
    with conn:
        conn.execute(
            """UPDATE linkedin_person_candidates SET status='expired'
                 WHERE lead_id=? AND workspace=? AND channel=? AND thread_ts=?
                   AND requested_by=? AND status='active'""",
            (lead_id, workspace, channel, thread_ts, requested_by),
        )
        conn.execute(
            """INSERT INTO linkedin_person_candidates
                 (id,lead_id,workspace,channel,thread_ts,requested_by,person_name,title,
                  profile_url,organization,evidence_excerpt,evidence_hash,status,
                  created_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active',?,?)""",
            (candidate_id, lead_id, workspace, channel, thread_ts, requested_by,
             person.name.strip(), person.title.strip(), person.url.strip(),
             organization.strip(), person.evidence_excerpt.strip(),
             _evidence_hash(person, organization.strip()), _iso(now), _iso(expires)),
        )
    return LinkedInCandidate(
        candidate_id, lead_id, workspace, channel, thread_ts, requested_by,
        person.name.strip(), person.title.strip(), person.url.strip(),
        organization.strip(), person.evidence_excerpt.strip(), _iso(expires))


def active_candidate(
        conn: sqlite3.Connection, lead_id: int, workspace: str, channel: str,
        thread_ts: str, requested_by: str) -> LinkedInCandidate | None:
    """Return exactly one unexpired candidate from the same tenant/thread/user."""
    now = _iso(_now())
    with conn:
        conn.execute(
            """UPDATE linkedin_person_candidates SET status='expired'
                 WHERE status='active' AND expires_at<=?""", (now,))
    rows = conn.execute(
        """SELECT * FROM linkedin_person_candidates
             WHERE lead_id=? AND workspace=? AND channel=? AND thread_ts=?
               AND requested_by=? AND status='active' AND expires_at>?
             ORDER BY created_at DESC LIMIT 2""",
        (lead_id, workspace, channel, thread_ts, requested_by, now),
    ).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return LinkedInCandidate(
        str(row["id"]), int(row["lead_id"]), str(row["workspace"]),
        str(row["channel"]), str(row["thread_ts"]), str(row["requested_by"]),
        str(row["person_name"]), str(row["title"] or ""),
        str(row["profile_url"]), str(row["organization"]),
        str(row["evidence_excerpt"]), str(row["expires_at"]),
    )


def get_candidate(
        conn: sqlite3.Connection, candidate_id: str, workspace: str, channel: str,
        thread_ts: str, requested_by: str) -> LinkedInCandidate:
    """Load one active candidate only from its exact tenant/thread/user context."""
    row = conn.execute(
        """SELECT * FROM linkedin_person_candidates
             WHERE id=? AND workspace=? AND channel=? AND thread_ts=?
               AND requested_by=? AND status='active' AND expires_at>?""",
        (candidate_id, workspace, channel, thread_ts, requested_by, _iso(_now())),
    ).fetchone()
    if row is None:
        raise ValueError("LinkedIn candidate is stale or belongs to another thread")
    return LinkedInCandidate(
        str(row["id"]), int(row["lead_id"]), str(row["workspace"]),
        str(row["channel"]), str(row["thread_ts"]), str(row["requested_by"]),
        str(row["person_name"]), str(row["title"] or ""),
        str(row["profile_url"]), str(row["organization"]),
        str(row["evidence_excerpt"]), str(row["expires_at"]),
    )


def consume_candidate(
        conn: sqlite3.Connection, candidate_id: str, action_id: str) -> None:
    """Mark one exact candidate consumed only after Salesforce verification succeeds."""
    with conn:
        changed = conn.execute(
            """UPDATE linkedin_person_candidates
                  SET status='consumed',consumed_action_id=?
                WHERE id=? AND status='active'""",
            (action_id, candidate_id),
        ).rowcount
    if changed != 1:
        raise ValueError("LinkedIn candidate was already consumed or expired")
