"""Shared fixtures for the follow-up suites.

Split out when `test_nudge_followups.py` hit the 1,000-line cap (CLAUDE.md rule 4).
These are deliberately shared rather than duplicated: the Slack double is the thing an
adversarial review found wanting — it could only ever emit what the code already looked
for, so no test could see a `file_share` reply, a paged thread or a reaction. Two copies
would mean fixing that twice, and the second copy is the one that would rot.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from grant_watch import db

CHANNEL = "C0TEST"
MANAGER = "U01DFJWQQJ3"  # Anthony, the one roster row carrying `manager: true`
JOCELYN = "U06RXJKRXSR"
BRETT = "U08C1NBH875"
# A Wednesday inside the business window (11:00 Pacific).
NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


class _Slack:
    """Slack stand-in whose thread contents the test controls.

    `human_reply_at` is the ts of a human message in the thread; None means the thread
    holds only Grant's own post, which is verified silence. `explode` makes every read
    fail, which is how an outage or a missing scope actually presents.
    """

    def __init__(
        self,
        *,
        human_reply_at: str | None = None,
        explode: bool = False,
        members: list[str] | None = None,
        subtype: str = "",
        reactions: list[dict[str, Any]] | None = None,
        pages: int = 1,
    ) -> None:
        """Record posts and serve a thread the test has described."""
        self.posts: list[dict[str, Any]] = []
        self.human_reply_at = human_reply_at
        self.explode = explode
        # The manager is in the channel unless a test says otherwise: an escalation
        # nobody can see is suppressed, so without this every escalation test would
        # pass for the wrong reason.
        self.members = members if members is not None else [MANAGER, JOCELYN]
        self.subtype = subtype
        self.reactions = reactions
        self.pages = pages
        self.pages_served = 0

    def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        """Who is in the channel, for the "can the manager see this?" guard."""
        if self.explode:
            raise RuntimeError("slack is down")
        return {"ok": True, "members": self.members}

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        """Record one post."""
        self.posts.append(kwargs)
        return {"ok": True, "ts": "999.1"}

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        """The thread as Slack would really return it — paged, oldest first.

        `pages` models a thread longer than one page. Slack returns replies OLDEST
        FIRST, so the reply is served on the LAST page, which is exactly the shape
        that made a truncated read report "verified silence".
        """
        if self.explode:
            raise RuntimeError("slack is down")
        self.pages_served += 1
        last_page = self.pages_served >= self.pages
        messages: list[dict[str, Any]] = [
            {"bot_id": "B1", "ts": "700.1", **({"reactions": self.reactions} if self.reactions else {})}
        ]
        if self.human_reply_at and last_page:
            reply: dict[str, Any] = {"user": JOCELYN, "ts": self.human_reply_at}
            if self.subtype:
                reply["subtype"] = self.subtype
            messages.append(reply)
        if last_page:
            return {"ok": True, "messages": messages}
        return {
            "ok": True,
            "messages": messages,
            "has_more": True,
            "response_metadata": {"next_cursor": f"page{self.pages_served}"},
        }

    def chat_getPermalink(self, **kwargs: Any) -> dict[str, Any]:
        """A working link, so the escalation can point at what it is about."""
        return {"ok": True, "permalink": "https://slack.example/archives/C0TEST/p1"}


def _conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated database of its own."""
    return db.connect(tmp_path / "followups.db")


def _delivered_offer(conn: sqlite3.Connection, when: datetime) -> str:
    """A `capability_now_available` follow-up that was delivered to Jocelyn.

    This is the exact shape of the real row behind Chase's case: Grant posted "I can
    build that campaign now — want me to?" into the channel thread where she had asked
    on 23 July, and the ledger recorded the delivery.
    """
    conn.execute(
        """INSERT INTO followup_nudges
             (id,subject_kind,subject_id,audience,target_slack,anchor_ts,
              policy_version,due_at,drop_after,state,observed_json,delivery_key,
              reserved_at,delivered_at,slack_ts,variant)
           VALUES ('offer-1','capability_now_available','5',?,?,'700.1',
                   'nudge-v1',?,?,'delivered',
                   '{"capability":"campaign_load","ask_text":"I want them both, the gold and the silver leads please","asked_on":"23 July"}',
                   'k1',?,?,'800.5','b')""",
        (
            CHANNEL,
            JOCELYN,
            when.isoformat(),
            (when + timedelta(days=14)).isoformat(),
            when.isoformat(),
            when.isoformat(),
        ),
    )
    conn.commit()
    return "offer-1"


def _card(
    conn: sqlite3.Connection,
    when: datetime,
    *,
    kind: str = "rich_award",
    style: str = "gold",
    lead_id: int = 900,
) -> None:
    """A posted lead card of one tier, which nobody engaged with."""
    conn.execute(
        "INSERT INTO leads (id,source,source_item_id,entity_name,state,detail_url,"
        "amount,status) VALUES (?,'usaspending:svpp',?,"
        "'NORTH PALOS SCHOOL DISTRICT 117','IL','u',500000,'new')",
        (lead_id, f"x{lead_id}"),
    )
    conn.execute(
        "INSERT INTO posts (id,channel,ts,posted_at,lead_id,kind,style) "
        "VALUES (?,?,?,?,?,?,?)",
        (lead_id, CHANNEL, f"8{lead_id}.1", when.isoformat(), lead_id, kind, style),
    )
    conn.commit()
