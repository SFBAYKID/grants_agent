"""The morning update: what Grant can newly do, in plain words, once.

WHY GRANT DELIVERS THIS RATHER THAN WRITES IT. The obvious build is "summarise the
recent commits at 8am", and it fails twice over: commit messages are written for
engineers, and a model summarising its own changelog will describe capabilities that
do not work yet. That exact failure is already in this project's history — Grant told
a rep "I'll keep watching those states and flag new awards here", nothing of the kind
existed, and she never used it again. An announcement that oversells is worse than no
announcement, because the first thing a rep tries is the thing that was promised.

So the words are AUTHORED and reviewed, stored verbatim, and Grant posts them. It
composes nothing.

THE POST IS ONLY HALF THE FEATURE. Chase: "It should not just be a one off post that
nobody reads." The other half already exists and needs no new code — an announcement
names the capabilities it covers, and `capability_asks` already knows which rep asked
for each one. Declaring those capabilities is what makes the follow-up worker reopen
their threads, so the channel post and the personal "you asked for this, it works
now" land from the same act.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


@dataclass(frozen=True)
class Announcement:
    """One authored update, exactly as it will be posted."""

    announcement_id: int
    slug: str
    audience: str
    body: str
    capabilities: tuple[str, ...]


def _now() -> str:
    """UTC timestamp in the repo's standard stored form."""
    return datetime.now(timezone.utc).isoformat()


def load(conn: sqlite3.Connection, path: Path) -> int:
    """Record authored announcements from a reviewed file. Returns how many are new.

    Idempotent on `slug`, so re-running a seed cannot post the same update twice.
    """
    body = json.loads(path.read_text())
    added = 0
    for item in body.get("announcements", []):
        text = str(item.get("body") or "").strip()
        slug = str(item.get("slug") or "").strip()
        if not text or not slug:
            raise ValueError("an announcement needs a slug and a body")
        try:
            conn.execute(
                """INSERT INTO announcements
                     (slug,audience,body,capabilities,created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    slug,
                    str(item.get("audience") or ""),
                    text,
                    ",".join(str(c) for c in item.get("capabilities", [])),
                    _now(),
                ),
            )
            added += 1
        except sqlite3.IntegrityError:
            # Already on file. Revise it while it is still UNPOSTED — these words are
            # authored and get corrected, and an overpromise found in review has to
            # be fixable without minting a new slug and leaving the wrong text queued
            # ahead of it. Once posted the row is frozen: people have read it, and
            # editing history is its own kind of dishonesty.
            conn.execute(
                """UPDATE announcements SET body=?, capabilities=?, audience=?
                    WHERE slug=? AND posted_at IS NULL""",
                (
                    text,
                    ",".join(str(c) for c in item.get("capabilities", [])),
                    str(item.get("audience") or ""),
                    slug,
                ),
            )
            continue
    conn.commit()
    return added


def pending(conn: sqlite3.Connection) -> Announcement | None:
    """The oldest announcement that has never been posted, or None."""
    row = conn.execute(
        "SELECT * FROM announcements WHERE posted_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    raw = str(row["capabilities"] or "")
    return Announcement(
        announcement_id=int(row["id"]),
        slug=str(row["slug"]),
        audience=str(row["audience"]),
        body=str(row["body"]),
        capabilities=tuple(part for part in raw.split(",") if part),
    )


def run(
    client: WebClient | None,
    conn: sqlite3.Connection,
    *,
    dry_run: bool = True,
) -> str:
    """Post the next unposted update, exactly once.

    Reserve-before-send, like every other proactive sender here: `posted_at` is
    written BEFORE the Slack call, so a crash between the two produces a missed
    update rather than a duplicate one. A repeated "here's what's new" is the fastest
    way to teach a channel to ignore Grant.
    """
    item = pending(conn)
    if item is None:
        return "skip: nothing to announce"
    if not item.audience:
        return f"skip: {item.slug} has no audience"
    if dry_run:
        return f"[dry-run] would post {item.slug} to {item.audience}:\n{item.body}"
    if client is None:
        return "skip: no Slack client configured"

    conn.execute(
        "UPDATE announcements SET posted_at=? WHERE id=? AND posted_at IS NULL",
        (_now(), item.announcement_id),
    )
    conn.commit()
    try:
        response = client.chat_postMessage(channel=item.audience, text=item.body)
    except SlackApiError as exc:
        code = str(exc.response.get("error") or "")
        # A permanent Slack refusal leaves posted_at set: retrying a channel-wide
        # post on a bad channel id helps nobody and risks a duplicate if the first
        # actually landed.
        return f"announce failed ({code})"
    except Exception as exc:  # noqa: BLE001 — ambiguity is preserved, never retried
        return f"announce ambiguous ({type(exc).__name__})"
    conn.execute(
        "UPDATE announcements SET slack_ts=? WHERE id=?",
        (str(response.get("ts") or ""), item.announcement_id),
    )
    conn.commit()
    return f"announced {item.slug} in {item.audience}"
