"""Read-only Salesforce follow-up monitor with fail-closed Slack delivery.

Only Campaign Members created by Grant's completed, immutable CRM action ledger are
eligible. Salesforce is queried through the GET-only reader and uncertainty always
suppresses notifications. The module never imports the Salesforce write gateway.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any  # Salesforce REST records are runtime-shaped JSON.
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

from .. import db
from ..enrich.salesforce import readonly_soql
from ..presentation import display_entity_name
from .drip import ABSOLUTE_CAP, in_window

POLICY_VERSION = "v1"
DEFAULT_GRACE_BUSINESS_DAYS = 3
BUSINESS_TZ = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class FollowupCandidate:
    """One locally proven Grant-created Campaign membership awaiting evaluation."""

    item_id: int
    campaign_member_id: str
    campaign_id: str
    target_sobject: str
    target_record_id: str
    entity_name: str
    joined_at: datetime
    due_at: datetime


@dataclass(frozen=True)
class ActivityResult:
    """Fail-closed classification of Salesforce activity after Campaign enrollment."""

    status: str
    evidence_kind: str = ""
    evidence_id: str = ""
    evidence_at: str = ""
    error: str = ""


def _parse_utc(value: str) -> datetime:
    """Parse one Salesforce/SQLite timestamp and normalize it to aware UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_business_days(start: datetime, days: int) -> datetime:
    """Return the same local wall time after a number of Monday-Friday days."""
    if days < 0:
        raise ValueError("business-day delay cannot be negative")
    local = start.astimezone(BUSINESS_TZ)
    remaining = days
    while remaining:
        local += timedelta(days=1)
        if local.weekday() < 5:
            remaining -= 1
    return local.astimezone(timezone.utc)


def _record_id(proposed_json: str, salesforce_id: str) -> tuple[str, str]:
    """Recover the frozen Lead/Contact target from one approved action item."""
    if salesforce_id:
        return "Lead", salesforce_id
    proposed: dict[str, Any] = json.loads(proposed_json)
    reference = proposed.get("salesforce_ref") or {}
    record_id = str(reference.get("record_id") or "")
    sobject = str(reference.get("sobject") or "")
    if sobject not in {"Lead", "Contact"} or not record_id:
        raise ValueError("approved Campaign Member target is missing")
    return sobject, record_id


def candidates(conn: sqlite3.Connection, grace_days: int = DEFAULT_GRACE_BUSINESS_DAYS
               ) -> list[FollowupCandidate]:
    """Return only memberships whose Campaign and member writes Grant completed."""
    rows = conn.execute(
        """SELECT i.id,i.campaign_member_id,i.proposed_json,i.salesforce_id,
                  a.campaign_id,a.committed_at,l.entity_name,a.payload_json
             FROM crm_action_items i
             JOIN crm_actions a ON a.id=i.action_id
             LEFT JOIN leads l ON l.id=i.lead_id
            WHERE a.action_type='add_campaign_members'
              AND a.state IN ('complete','partial')
              AND i.state='added' AND i.campaign_member_id IS NOT NULL
              AND a.committed_at IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM crm_actions c
                   WHERE c.action_type='create_campaign' AND c.state='complete'
                     AND c.campaign_id=a.campaign_id)
            ORDER BY a.committed_at,i.id""").fetchall()
    result: list[FollowupCandidate] = []
    for row in rows:
        try:
            sobject, record_id = _record_id(
                str(row["proposed_json"]), str(row["salesforce_id"] or ""))
            joined = _parse_utc(str(row["committed_at"]))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        result.append(FollowupCandidate(
            int(row["id"]), str(row["campaign_member_id"]), str(row["campaign_id"]),
            sobject, record_id, str(row["entity_name"] or "this organization"),
            joined, add_business_days(joined, grace_days)))
    return result


def _sf_datetime(record: dict[str, Any], *fields: str) -> datetime | None:
    """Return the first parseable Salesforce date/datetime field."""
    for field in fields:
        raw = str(record.get(field) or "")
        if not raw:
            continue
        try:
            if len(raw) == 10:
                return datetime.combine(date.fromisoformat(raw), time.min, timezone.utc)
            return _parse_utc(raw)
        except ValueError:
            return None
    return None


def inspect_activity(candidate: FollowupCandidate, now: datetime) -> ActivityResult:
    """Read exact member, target, Tasks, and Events; any read failure is unknown."""
    record_id = candidate.target_record_id.replace("'", "")
    member_id = candidate.campaign_member_id.replace("'", "")
    try:
        members, _ = readonly_soql(
            "SELECT Id,HasResponded FROM CampaignMember "
            f"WHERE Id='{member_id}' LIMIT 1")
        targets, _ = readonly_soql(
            f"SELECT Id,LastActivityDate FROM {candidate.target_sobject} "
            f"WHERE Id='{record_id}' LIMIT 1")
        tasks, _ = readonly_soql(
            "SELECT Id,IsClosed,ActivityDate,CompletedDateTime FROM Task "
            f"WHERE WhoId='{record_id}'")
        events, _ = readonly_soql(
            "SELECT Id,EndDateTime FROM Event "
            f"WHERE WhoId='{record_id}'")
    except Exception as exc:  # noqa: BLE001 — outages must suppress Slack
        return ActivityResult("unknown", error=type(exc).__name__)
    if len(members) != 1 or len(targets) != 1:
        return ActivityResult("unknown", error="record_missing_or_ambiguous")
    if bool(members[0].get("HasResponded")):
        return ActivityResult("activity", "campaign_response", str(members[0].get("Id") or ""))
    for task in tasks:
        happened = _sf_datetime(task, "CompletedDateTime", "ActivityDate")
        if bool(task.get("IsClosed")) and happened and happened >= candidate.joined_at:
            return ActivityResult("activity", "task", str(task.get("Id") or ""), happened.isoformat())
    for event in events:
        happened = _sf_datetime(event, "EndDateTime")
        if happened and candidate.joined_at <= happened <= now:
            return ActivityResult("activity", "event", str(event.get("Id") or ""), happened.isoformat())
    last_activity = _sf_datetime(targets[0], "LastActivityDate")
    if last_activity and last_activity.date() >= candidate.joined_at.date():
        return ActivityResult("activity", "activity_date_only", evidence_at=last_activity.date().isoformat())
    return ActivityResult("none")


def build_message(candidate: FollowupCandidate) -> str:
    """Build one short, user-focused Slack sentence without technical details."""
    entity = display_entity_name(candidate.entity_name) or "This organization"
    return f"{entity} still needs follow-up in Salesforce."


def _used_slots(conn: sqlite3.Connection, channel: str, now: datetime) -> int:
    """Count normal proactive posts plus reserved follow-up deliveries today."""
    start = datetime.combine(now.date(), time.min, timezone.utc).isoformat()
    followups = conn.execute(
        """SELECT COUNT(*) FROM salesforce_followup_state
            WHERE state IN ('sending','delivered','unknown')
              AND checked_at>=?""", (start,)).fetchone()[0]
    return len(db.posts_today(conn, channel, now)) + int(followups)


def run(client: WebClient | None, channel: str, conn: sqlite3.Connection,
        dry_run: bool = False, smoke: bool = False,
        now: datetime | None = None, grace_days: int = DEFAULT_GRACE_BUSINESS_DAYS) -> str:
    """Evaluate candidates and deliver at most one deduplicated Slack reminder."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for candidate in candidates(conn, grace_days):
        if not smoke and current < candidate.due_at:
            continue
        existing = conn.execute(
            "SELECT state FROM salesforce_followup_state WHERE campaign_member_id=?",
            (candidate.campaign_member_id,)).fetchone()
        if existing is not None:
            continue
        activity = inspect_activity(candidate, current)
        if activity.status != "none":
            if not dry_run:
                with conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO salesforce_followup_state
                           (campaign_member_id,crm_action_item_id,campaign_id,target_sobject,
                            target_record_id,joined_at,due_at,policy_version,state,evidence_kind,
                            evidence_id,evidence_at,checked_at,last_error)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (candidate.campaign_member_id,candidate.item_id,candidate.campaign_id,
                         candidate.target_sobject,candidate.target_record_id,
                         candidate.joined_at.isoformat(),candidate.due_at.isoformat(),POLICY_VERSION,
                         "activity_seen" if activity.status == "activity" else "unknown",
                         activity.evidence_kind or None,activity.evidence_id or None,
                         activity.evidence_at or None,current.isoformat(),activity.error or None))
            continue
        text = build_message(candidate)
        if dry_run:
            return f"[dry-run] would post: {text}"
        if not smoke and not in_window(current):
            return "skip: outside business hours"
        delivery_key = f"sf-followup:{candidate.campaign_member_id}:{POLICY_VERSION}"
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _used_slots(conn, channel, current) >= ABSOLUTE_CAP:
                conn.rollback()
                return f"skip: absolute daily cap reached ({ABSOLUTE_CAP})"
            inserted = conn.execute(
                """INSERT OR IGNORE INTO salesforce_followup_state
                   (campaign_member_id,crm_action_item_id,campaign_id,target_sobject,
                    target_record_id,joined_at,due_at,policy_version,state,checked_at,delivery_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate.campaign_member_id,candidate.item_id,candidate.campaign_id,
                 candidate.target_sobject,candidate.target_record_id,
                 candidate.joined_at.isoformat(),candidate.due_at.isoformat(),POLICY_VERSION,
                 "sending",current.isoformat(),delivery_key)).rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if inserted != 1:
            continue
        assert client is not None
        try:
            identity = client.auth_test()
            workspace = str(identity.get("team_id") or "")
            bot_user = str(identity.get("user_id") or "Grant")
            if not workspace:
                raise RuntimeError("Slack workspace identity unavailable")
            response = client.chat_postMessage(channel=channel, text=text, mrkdwn=False,
                                               unfurl_links=False, unfurl_media=False)
            db.register_conversation_thread(
                conn, workspace, channel, str(response["ts"]), bot_user)
        except Exception as exc:  # noqa: BLE001 — ambiguous sends are never retried
            with conn:
                conn.execute(
                    """UPDATE salesforce_followup_state
                       SET state='unknown',last_error=?,checked_at=?
                       WHERE campaign_member_id=?""",
                    (type(exc).__name__,current.isoformat(),candidate.campaign_member_id))
            return "unknown: Slack delivery could not be confirmed; no automatic retry"
        with conn:
            conn.execute(
                """UPDATE salesforce_followup_state
                   SET state='delivered',slack_ts=?,delivered_at=?,checked_at=?
                   WHERE campaign_member_id=?""",
                (str(response["ts"]),current.isoformat(),current.isoformat(),
                 candidate.campaign_member_id))
        return f"posted follow-up reminder for Campaign Member {candidate.campaign_member_id}"
    return "skip: no untouched Grant-created Campaign Members are due"
