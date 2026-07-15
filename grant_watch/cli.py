"""Command-line entrypoints for grant_watch.

Usage (from the repo root, venv active):
    python -m grant_watch.cli poll [--source NAME] [--dry-run]
    python -m grant_watch.cli seed
    python -m grant_watch.cli status
    python -m grant_watch.cli drip [--force] [--dry-run]   # drip tick (30-min cron target)
    python -m grant_watch.cli slack-smoke [--dry-run]      # release connectivity check
    python -m grant_watch.cli slack-failures [--mark-reviewed EVENT_ID]

--dry-run polls and grades but writes NOTHING (no DB rows, no run log) — required by
CLAUDE.md for anything that will later feed Slack. Errors in one source never abort
the others; they are printed and recorded in the `runs` table.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import db, scoring
from .models import RawItem, RunStats
from .sources import POLLERS, sam_gov

SEED_CSV = Path(__file__).resolve().parent.parent / "data" / "svpp_active_awards_CA_MI_PA_WA.csv"


Poller = Callable[[], list[RawItem]]


def _active_pollers() -> list[tuple[str, Poller]]:
    """The static registry plus SAM.gov when its key is configured."""
    pollers = list(POLLERS)
    sam_key = os.environ.get("SAM_API_KEY", "")
    if sam_key:
        pollers.append(("SAM.gov", lambda: sam_gov.poll(sam_key)))
    else:
        print("[skip] SAM.gov — set SAM_API_KEY in .env to enable", file=sys.stderr)
    return pollers


def _redact_error(exc: Exception) -> str:
    """Return an error summary with configured secrets and URL API keys removed."""
    message = f"{type(exc).__name__}: {exc}"
    for key in ("SAM_API_KEY", "FIRECRAWL_API_KEY", "SALESFORCE_CLIENT_SECRET"):
        value = os.environ.get(key, "")
        if value:
            message = message.replace(value, "[REDACTED]")
    return re.sub(r"(?i)(api_key=)[^&\s]+", r"\1[REDACTED]", message)


def cmd_poll(only_source: str | None, dry_run: bool) -> int:
    """Run selected pollers; return failure when any selected source is incomplete."""
    conn = None if dry_run else db.connect()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errors = 0
    selected = 0
    lock_owner = str(uuid.uuid4())
    if conn is not None and not db.acquire_poll_lock(conn, "poll", lock_owner):
        print("poll already running; refusing overlapping source writes", file=sys.stderr)
        return 2

    try:
        for name, poll_fn in _active_pollers():
            if only_source and only_source.lower() not in name.lower():
                continue
            selected += 1
            stats = RunStats(source=name)
            try:
                items = poll_fn()
                stats.items_seen = len(items)
                for item in items:
                    lead = scoring.grade(item)
                    if dry_run:
                        continue
                    assert conn is not None
                    if db.upsert_lead(conn, lead):
                        stats.items_new += 1
                        fresh = " [FRESH]" if scoring.is_fresh(item) else ""
                        amt = f" ${item.amount:,.0f}" if item.amount else ""
                        print(f"  NEW {lead.grade.value.upper():6s} [{item.source}] "
                              f"{item.entity}{amt} — {item.title[:70]}{fresh}")
            except Exception as exc:  # continue other sources, but fail the command
                stats.errors = _redact_error(exc)
                stats.complete = False
                stats.error_code = type(exc).__name__
                errors += 1
                print(f"[{name}] ERROR: {stats.errors}", file=sys.stderr)
            if conn is not None:
                db.log_run(conn, started, stats)
            print(f"[{name}] {stats.items_seen} items, {stats.items_new} new"
                  f"{' (dry-run: nothing written)' if dry_run else ''}")
        if selected == 0:
            print("no poller matched --source", file=sys.stderr)
            return 2
        if conn is not None:
            retired = db.reconcile_seed_duplicates(conn)
            if retired:
                print(f"[reconcile] {retired} seed rows superseded by live award rows")
        return 1 if errors else 0
    finally:
        if conn is not None:
            db.release_poll_lock(conn, "poll", lock_owner)


def cmd_seed() -> int:
    """Seed the 75 verified SVPP GOLD awards from the data CSV (idempotent)."""
    conn = db.connect()
    rows, new = db.seed_from_csv(conn, SEED_CSV)
    print(f"seed: {rows} rows in CSV, {new} inserted (rest already present)")
    return 0


def cmd_status() -> int:
    """Print lead counts by source and grade."""
    conn = db.connect()
    for source, grade_, count in db.status_summary(conn):
        print(f"{source:24s} {grade_:7s} {count}")
    return 0


def cmd_drip(force: bool, dry_run: bool) -> int:
    """One drip tick: Grant decides whether to surface one nugget/bulletin now.
    Designed for a 30-minute cron, Mon-Fri, inside the 8am ET - 5pm PT window."""
    from slack_sdk import WebClient

    from .slack import drip as drip_mod

    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    if not channel:
        print("SLACK_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1
    client = None if dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    conn = db.connect_readonly() if dry_run else db.connect()
    outcome = drip_mod.run_drip(client, channel, conn,
                                force=force, dry_run=dry_run)
    print(f"drip: {outcome}")
    return 1 if outcome.startswith("unknown:") else 0


def cmd_outreach_retry(dry_run: bool) -> int:
    """Run one bounded Persequor retry pass using persisted idempotency keys."""
    from . import persequor_client

    conn = db.connect_readonly() if dry_run else db.connect()
    summary = persequor_client.retry_pending(conn, dry_run=dry_run)
    print(f"outreach retry: {summary.due} due, {summary.submitted} submitted, "
          f"{summary.queued} queued, {summary.rejected} rejected"
          f"{' (dry-run: no requests or writes)' if dry_run else ''}")
    return 1 if not dry_run and (summary.queued or summary.rejected) else 0


def cmd_salesforce_sync(limit: int, dry_run: bool) -> int:
    """Refresh local CRM context through the strictly read-only Salesforce reader."""
    from .enrich import salesforce_sync

    conn = db.connect_readonly() if dry_run else db.connect()
    summary = salesforce_sync.sync(conn, limit=limit, dry_run=dry_run)
    print(f"salesforce sync: {summary.checked} checked; {summary.found} found, "
          f"{summary.no_match} no-match, {summary.ambiguous} ambiguous, "
          f"{summary.partial} partial, {summary.unavailable} unavailable; "
          f"{summary.writes} local snapshots written"
          f"{' (dry-run)' if dry_run else ''}")
    return 1 if summary.partial or summary.unavailable else 0


def cmd_slack_smoke(dry_run: bool) -> int:
    """Post one labeled, non-lead Slack release check after an optional dry run."""
    from slack_sdk import WebClient

    from .slack import smoke

    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    if not channel:
        print("SLACK_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1
    client = None if dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    print(smoke.post_smoke(client, channel, dry_run=dry_run))
    return 0


def cmd_slack_failures(mark_reviewed: str = "") -> int:
    """List unresolved Slack turns or mark one manually reconciled without replay."""
    conn = db.connect() if mark_reviewed else db.connect_readonly()
    if mark_reviewed:
        if not db.mark_slack_event_reviewed(conn, mark_reviewed):
            print("Slack event was not pending reconciliation", file=sys.stderr)
            return 2
        print(f"Slack event {mark_reviewed} marked reviewed; no action was replayed")
        return 0
    rows = db.unresolved_slack_events(conn)
    if not rows:
        print("Slack failures: none pending reconciliation")
        return 0
    for row in rows:
        print(
            f"{row['event_id']} channel={row['channel']} thread={row['thread_ts'] or '-'} "
            f"action={row['action_state']} delivery={row['delivery_state']} "
            f"error={row['error'] or 'unknown'}")
    print(f"Slack failures: {len(rows)} pending manual reconciliation", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch. .env is loaded here so every command sees the keys."""
    load_dotenv()
    parser = argparse.ArgumentParser(prog="grant_watch", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_poll = sub.add_parser("poll", help="poll all sources for new leads")
    p_poll.add_argument("--source", help="only run sources whose name contains this")
    p_poll.add_argument("--dry-run", action="store_true",
                        help="poll and grade but write nothing")
    sub.add_parser("seed", help="seed leads from the verified SVPP CSV")
    sub.add_parser("status", help="lead counts by source and grade")
    p_drip = sub.add_parser("drip", help="one drip tick (maybe post one nugget)")
    p_drip.add_argument("--force", action="store_true",
                        help="bypass window/cap/jitter pacing (for testing)")
    p_drip.add_argument("--dry-run", action="store_true",
                        help="print what would post; write nothing")
    p_retry = sub.add_parser(
        "outreach-retry", help="retry due Persequor handoffs idempotently")
    p_retry.add_argument("--dry-run", action="store_true",
                         help="count due requests; send and write nothing")
    p_sf = sub.add_parser(
        "salesforce-sync", help="refresh read-only Salesforce lead context")
    p_sf.add_argument("--limit", type=int, default=25,
                      help="maximum leads to check (1-100; default 25)")
    p_sf.add_argument("--dry-run", action="store_true",
                      help="query Salesforce but write no local snapshots")
    p_smoke = sub.add_parser(
        "slack-smoke", help="post one clearly labeled release test to Slack")
    p_smoke.add_argument("--dry-run", action="store_true",
                         help="show the test message without posting")
    p_failures = sub.add_parser(
        "slack-failures", help="list Slack turns needing manual reconciliation")
    p_failures.add_argument(
        "--mark-reviewed", default="", metavar="EVENT_ID",
        help="acknowledge one inspected event without replaying it")

    args = parser.parse_args(argv)
    if args.command == "poll":
        return cmd_poll(args.source, args.dry_run)
    if args.command == "seed":
        return cmd_seed()
    if args.command == "drip":
        return cmd_drip(args.force, args.dry_run)
    if args.command == "outreach-retry":
        return cmd_outreach_retry(args.dry_run)
    if args.command == "salesforce-sync":
        return cmd_salesforce_sync(args.limit, args.dry_run)
    if args.command == "slack-smoke":
        return cmd_slack_smoke(args.dry_run)
    if args.command == "slack-failures":
        return cmd_slack_failures(args.mark_reviewed)
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
