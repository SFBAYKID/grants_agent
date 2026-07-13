"""Weekly digest: turn digest_leads() buckets into Slack Block Kit and post them.

Design: build_digest_blocks() is PURE (rows in, blocks out) so tests cover the exact
payload without a Slack connection. Slack caps messages at 50 blocks, so each bucket
is capped and the remainder is summarized in a count line — never silently dropped
(CLAUDE.md: no silent truncation).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from slack_sdk import WebClient

from .. import db, scoring

# Per-bucket caps keep the message under Slack's 50-block limit (3 blocks/lead worst case).
GOLD_CAP = 8
SILVER_CAP = 4
EXPIRING_CAP = 4

# The four triage buttons attached to every lead (action_id -> label).
_BUTTONS = (
    ("grant_draft_email", "✉️ Draft email"),
    ("grant_mark_contacted", "✅ Mark contacted"),
    ("grant_snooze", "💤 Snooze"),
    ("grant_bad_lead", "👎 Bad lead"),
)


def _fmt_amount(amount: float | None) -> str:
    return f"${amount:,.0f}" if amount else "$ n/a"


def _why_now(row: sqlite3.Row) -> str:
    """One honest 'why now' line per lead — derived only from fields we actually have."""
    if row["funds_end"]:
        return f"spend window open through {row['funds_end']}"
    if row["lead_grade"] == "silver":
        return "open solicitation — respond before the close date"
    return "newly seen this week"


def _lead_blocks(row: sqlite3.Row) -> list[dict[str, Any]]:
    """Blocks for one lead: a text section (+ deep link) and the four triage buttons.
    Contact line is only shown when enrichment (Phase 2) has stored one — never guessed."""
    medal = {"gold": "🥇", "silver": "🥈"}.get(row["lead_grade"], "👀")
    link = f"\n<{row['detail_url']}|source record>" if row["detail_url"] else ""
    text = (f"{medal} *{row['entity_name']}* ({row['state'] or '?'}) — "
            f"{row['program'] or row['source']} · {_fmt_amount(row['amount'])}\n"
            f"_{_why_now(row)}_{link}")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "block_id": f"lead-{row['id']}",
         "elements": [
             {"type": "button", "action_id": action_id,
              "text": {"type": "plain_text", "text": label, "emoji": True},
              "value": str(row["id"])}
             for action_id, label in _BUTTONS
         ]},
    ]


def _bucket(blocks: list[dict[str, Any]], title: str, rows: list[sqlite3.Row],
            cap: int) -> list[int]:
    """Append one capped bucket; return the lead ids actually shown."""
    if not rows:
        return []
    shown = rows[:cap]
    extra = f"  _(+{len(rows) - cap} more in the database)_" if len(rows) > cap else ""
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn", "text": f"*{title}* ({len(rows)}){extra}"}})
    for row in shown:
        blocks.extend(_lead_blocks(row))
    blocks.append({"type": "divider"})
    return [int(r["id"]) for r in shown]


def _rank(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Quality gate: order GOLD by lead_score (freshness x $ x program fit) so the
    capped digest always shows the STRONGEST leads, not the first-queried ones."""
    return sorted(rows,
                  key=lambda r: scoring.lead_score(r["program"], r["amount"],
                                                   r["funds_start"] or ""),
                  reverse=True)


def build_digest_blocks(buckets: dict[str, list[sqlite3.Row]]
                        ) -> tuple[list[dict[str, Any]], list[int]]:
    """Pure builder: buckets -> (blocks, ids of every lead shown)."""
    blocks: list[dict[str, Any]] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🦉 Grant — weekly lead digest",
                  "emoji": True}},
    ]
    shown: list[int] = []
    shown += _bucket(blocks, "🥇 New GOLD — just got security money",
                     _rank(buckets["gold"]), GOLD_CAP)
    shown += _bucket(blocks, "🥈 New SILVER — open RFPs", buckets["silver"], SILVER_CAP)
    shown += _bucket(blocks, "⏳ Expiring windows (<90 days) — use it or lose it",
                     buckets["expiring"], EXPIRING_CAP)
    if not shown:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": "No new leads this week — all quiet."}})
    return blocks, shown


def post_digest(client: WebClient, channel: str, conn: sqlite3.Connection,
                dry_run: bool = False) -> int:
    """Build and post the digest; mark the SHOWN new leads as surfaced.

    Returns the number of leads shown. --dry-run prints the block payload and writes
    nothing anywhere (no Slack post, no status flips).
    """
    buckets = db.digest_leads(conn)
    blocks, shown_ids = build_digest_blocks(buckets)
    if dry_run:
        import json
        print(json.dumps(blocks, indent=1))
        print(f"[dry-run] would post {len(shown_ids)} leads; nothing written")
        return len(shown_ids)
    client.chat_postMessage(channel=channel, blocks=blocks,
                            text="Grant — weekly lead digest")  # text = notification fallback
    db.mark_surfaced(conn, shown_ids)
    return len(shown_ids)
