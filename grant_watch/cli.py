"""Command-line entrypoints for grant_watch.

Usage (from the repo root, venv active):
    python -m grant_watch.cli poll [--source NAME] [--dry-run]
    python -m grant_watch.cli seed
    python -m grant_watch.cli status
    python -m grant_watch.cli drip [--force] [--dry-run]   # drip tick (30-min cron target)
    python -m grant_watch.cli drip-blocked                 # leads set aside, never delivered
    python -m grant_watch.cli drip-unblock [--channel ID]  # clear a channel-level block
    python -m grant_watch.cli outreach-retry [--dry-run]
    python -m grant_watch.cli salesforce-sync [--limit N] [--dry-run]
    python -m grant_watch.cli nudge [--dry-run | --execute]
    python -m grant_watch.cli salesforce-followups [--dry-run] [--smoke]
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
from .config import primary_channel_id
from .models import RawItem, RunStats
from .sources import POLLERS, sam_gov

SEED_CSV = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "svpp_active_awards_CA_MI_PA_WA.csv"
)


Poller = Callable[[], list[RawItem]]


def _active_pollers() -> list[tuple[str, Poller]]:
    """The static registry plus SAM.gov when its key is configured."""
    pollers = list(POLLERS)
    sam_key = os.environ.get("SAM_API_KEY", "")
    if sam_key:
        pollers.append(("SAM.gov", lambda: sam_gov.poll(sam_key)))
    else:
        print("[skip] SAM.gov — set SAM_API_KEY in .env to enable", file=sys.stderr)
    # RFP discovery is paid, so it is opt-in (like SAM.gov's key gate) and never in the
    # free static POLLERS list. The AGGREGATOR source (Starbridge listing) is the one
    # wired: it finds the OPEN target-state RFPs cheaply (one scrape). The `.gov`-page
    # hunt (sources/rfp.py) is kept available but not wired — it found ~0 open pages
    # (Chase 2026-07-18: most individual RFP pages are already closed).
    if os.environ.get("RFP_DISCOVERY_ENABLED", "").strip() in ("1", "true", "yes"):
        from .sources import rfp_aggregator

        pollers.append(("Security RFP discovery", rfp_aggregator.poll))
    else:
        print(
            "[skip] Security RFP discovery — set RFP_DISCOVERY_ENABLED=1 to enable",
            file=sys.stderr,
        )
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
        print(
            "poll already running; refusing overlapping source writes", file=sys.stderr
        )
        return 2

    try:
        for name, poll_fn in _active_pollers():
            if only_source and only_source.lower() not in name.lower():
                continue
            selected += 1
            stats = RunStats(source=name)
            # Open the run BEFORE processing so confirmation freshness can bind to it,
            # but advance freshness ONLY on a durably-complete success (Chase A1).
            run_id = db.begin_run(conn, name, started) if conn is not None else None
            confirmed_keys: list[tuple[str, str]] = []
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
                        print(
                            f"  NEW {lead.grade.value.upper():6s} [{item.source}] "
                            f"{item.entity}{amt} — {item.title[:70]}{fresh}"
                        )
                    # Every item SEEN in this run (new or unchanged) is a candidate for
                    # confirmation, applied only if the run completes successfully.
                    confirmed_keys.append((item.source, str(item.item_id)))
            except Exception as exc:  # continue other sources, but fail the command
                stats.errors = _redact_error(exc)
                stats.complete = False
                stats.error_code = type(exc).__name__
                errors += 1
                print(f"[{name}] ERROR: {stats.errors}", file=sys.stderr)
            if conn is not None and run_id is not None:
                if stats.complete:
                    db.complete_run(conn, run_id, stats, confirmed_keys)
                else:
                    db.fail_run(conn, run_id, stats)
            print(
                f"[{name}] {stats.items_seen} items, {stats.items_new} new"
                f"{' (dry-run: nothing written)' if dry_run else ''}"
            )
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
    """One drip tick: Grant decides whether to surface one opportunity now.
    Designed for a 30-minute cron, Mon-Fri, inside the 7am ET - 5pm PT window."""
    from slack_sdk import WebClient

    from .campaign import rich_card_enabled
    from .slack import drip as drip_mod

    # Proactive drip always posts to the PRIMARY channel (first configured id).
    channel = primary_channel_id()
    if not channel:
        print("SLACK_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1
    client = None if dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    conn = db.connect_readonly() if dry_run else db.connect()
    if rich_card_enabled():
        from .campaign import delivery as rich_delivery

        members = (
            rich_delivery.channel_members(client, channel)
            if client is not None
            else frozenset()
        )
        outcome = rich_delivery.run(
            client,
            channel,
            conn,
            channel_members=members,
            force=force,
            dry_run=dry_run,
        )
        if rich_delivery.fallback_to_daily(outcome):
            # Chase 2026-08-05: a card lands every weekday in the newest look. When
            # the rich path provably cannot post today (no card satisfies the evidence
            # rules, or the rich cutoff has passed), the restyled daily card takes the
            # tick. Cap, guard, ambiguous-send, and waiting-for-slot outcomes never
            # fall through — see delivery.fallback_to_daily. The final outcome (and so
            # the exit status) is the path that actually ran.
            print(f"drip[rich]: {outcome}; falling back to the daily card")
            outcome = drip_mod.run_drip(
                client, channel, conn, force=force, dry_run=dry_run
            )
    else:
        outcome = drip_mod.run_drip(client, channel, conn, force=force, dry_run=dry_run)
    print(f"drip: {outcome}")
    # Non-zero for every outcome a human needs to look at, so cron surfaces it rather
    # than the failure reading as a routine tick. `blocked:` in particular repeats every
    # 30 minutes until an operator clears it and must never look like success.
    return 1 if outcome.startswith(FAILING_DRIP_OUTCOMES) else 0


# Drip outcomes that mean "a human should look at this". Everything else — a skip for
# the cap, the slot, or an empty pool — is a normal tick.
FAILING_DRIP_OUTCOMES = ("unknown:", "blocked:", "error:", "quarantined:")


def cmd_rich_shadow(limit: int, channel_members: list[str]) -> int:
    """Print a deterministic, PII-free, write-free readiness report."""
    from .campaign import preparation, report

    channel = primary_channel_id()
    if not channel:
        print("SLACK_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1
    conn = db.connect_readonly()
    reviews = preparation.review_candidates(
        conn,
        channel,
        frozenset(channel_members),
        limit=limit,
    )
    print(report.to_json(report.build(reviews)))
    return 0


def cmd_rich_prepare(limit: int, execute: bool, retry_indeterminate: bool) -> int:
    """Run or safely preview the bounded evidence-preparation worker."""
    from .campaign import prepare_worker

    channel = primary_channel_id()
    if not channel:
        print("SLACK_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1
    conn = db.connect() if execute else db.connect_readonly()
    summary = prepare_worker.run(
        conn,
        channel,
        limit=limit,
        dry_run=not execute,
        retry_indeterminate=retry_indeterminate,
    )
    print(
        f"rich prepare: {summary.candidates} candidates; "
        f"{summary.contact_fresh} contact-fresh, "
        f"{summary.contact_refreshed} contact-refreshed, "
        f"{summary.website_checked} website-checked, "
        f"{summary.crm_checked} crm-checked, "
        f"{summary.activity_checked} activity-checked, "
        f"{summary.indeterminate} indeterminate, {summary.errors} errors, "
        f"{summary.writes} local writes"
        f"{' (preview: no HTTP or writes)' if not execute else ''}"
    )
    return 1 if summary.indeterminate or summary.errors else 0


def cmd_nces_bind(limit_states: int, dry_run: bool) -> int:
    """Bind authoritative NCES district identity to Gold leads that still lack it.

    `leads.nces_id` is the ONLY input to the rich card's entity-kind gate, and the only
    thing that ever wrote it was a side effect of one shape of Slack search. On
    2026-08-06 that left 176 of production's 184 Gold candidates permanently rejected
    `entity_kind_unsupported`, which is why no rich card had ever posted. This is the
    schedulable binder for that column. It is free and keyless (NCES ArcGIS), and it
    relaxes nothing: `match_district` still demands a unique exact normalized-name
    match, so an ambiguous name binds nothing and the lead simply stays ineligible.

    States are visited most-pending first so one bounded run covers the most leads.
    """
    from .enrich import nces

    conn = db.connect_readonly() if dry_run else db.connect()
    states = [
        str(row["state"]).upper()
        for row in conn.execute(
            """SELECT state, COUNT(*) AS pending FROM leads
               WHERE lead_grade='gold' AND COALESCE(status,'new')='new'
                 AND (nces_id IS NULL OR nces_id='')
                 AND LENGTH(COALESCE(state,''))=2
               GROUP BY state ORDER BY pending DESC, state"""
        )
    ][: max(1, limit_states)]
    if dry_run:
        listed = ", ".join(states) or "(none)"
        print(f"nces bind: would fetch {len(states)} state(s): {listed}")
        return 0
    matched = considered = failures = 0
    for state in states:
        try:
            summary = nces.enrich_state_leads(conn, state)
        except Exception as exc:  # noqa: BLE001 - one bad state cannot abort the run
            failures += 1
            print(f"nces bind: {state} FAILED ({type(exc).__name__}: {exc})")
            continue
        considered += summary.candidates
        matched += summary.matched
        print(f"nces bind: {state} bound {summary.matched}/{summary.candidates}")
    print(
        f"nces bind: {matched}/{considered} leads bound across "
        f"{len(states) - failures} state(s); {failures} failed"
    )
    return 1 if failures else 0


def cmd_drip_unblock(channel: str) -> int:
    """Clear a channel-level block after an operator has fixed Slack.

    A systemic Slack failure (`channel_not_found`, `invalid_auth`, …) blocks the channel
    for an escalating 1h-8h period, because retrying every 30 minutes cannot help and
    used to consume a lead each time. The guard expires on its own; this command only
    resumes SOONER, once an operator knows Slack is fixed."""
    conn = db.connect()
    target = channel or primary_channel_id()
    if not target:
        print("no channel given and SLACK_CHANNEL_ID is not set", file=sys.stderr)
        return 1
    if db.clear_channel_guard(conn, target):
        print(f"cleared the block on {target}; the next tick will post normally")
        return 0
    print(f"no block was set on {target}")
    return 0


def cmd_drip_blocked() -> int:
    """List leads that were set aside and never delivered.

    Every non-delivered outbox row permanently excludes a real lead from the proactive
    pool. Without this surface, silent inventory loss is indistinguishable from a quiet
    week — which is exactly how a wedged drip went undiagnosed for a day.
    """
    conn = db.connect_readonly()
    rows = db.blocked_notifications(conn)
    # Channel guards live in the same table with a NULL lead_id. Reporting them as
    # leads printed "lead #None … (unknown entity)" — presenting an outage as a phantom
    # lost lead, and never naming the remedy.
    guards = [r for r in rows if r["lead_id"] is None]
    leads = [r for r in rows if r["lead_id"] is not None]
    if not rows:
        print("no blocked notifications: every reserved delivery was confirmed")
        return 0
    if guards:
        print(f"{len(guards)} CHANNEL GUARD(S) ACTIVE OR RECENT:\n")
        for row in guards:
            print(
                f"  channel {row['audience']}  [{row['state']}]  "
                f"reason={row['last_error'] or 'unrecorded'}\n"
                f"      first_failure={row['created_at']}  latest={row['updated_at']}  "
                f"holds_until={row['available_at']}\n"
                f"      -> resume early with: cli drip-unblock --channel "
                f"{row['audience']}"
            )
        print()
    if leads:
        print(f"{len(leads)} lead(s) set aside and never delivered:\n")
    for row in leads:
        entity = row["entity_name"] or "(unknown entity)"
        reason = row["last_error"] or "no reason recorded"
        print(
            f"  lead #{row['lead_id']}  [{row['state']}]  {entity} "
            f"({row['lead_state'] or '??'})\n"
            f"      channel={row['audience']}  at={row['created_at']}  reason={reason}"
        )
    print(
        "\n'unknown' may have reached Slack and is never auto-retried. 'rejected' and "
        "'unrenderable' provably did not. Clearing any of these is a human decision."
    )
    return 0


def cmd_outreach_retry(dry_run: bool) -> int:
    """Run one bounded Persequor retry pass using persisted idempotency keys."""
    from . import persequor_client

    conn = db.connect_readonly() if dry_run else db.connect()
    summary = persequor_client.retry_pending(conn, dry_run=dry_run)
    print(
        f"outreach retry: {summary.due} due, {summary.submitted} submitted, "
        f"{summary.queued} queued, {summary.rejected} rejected"
        f"{' (dry-run: no requests or writes)' if dry_run else ''}"
    )
    return 1 if not dry_run and (summary.queued or summary.rejected) else 0


def cmd_salesforce_sync(limit: int, dry_run: bool) -> int:
    """Refresh local CRM context through the strictly read-only Salesforce reader."""
    from .enrich import salesforce_sync

    conn = db.connect_readonly() if dry_run else db.connect()
    summary = salesforce_sync.sync(conn, limit=limit, dry_run=dry_run)
    print(
        f"salesforce sync: {summary.checked} checked; {summary.found} found, "
        f"{summary.no_match} no-match, {summary.ambiguous} ambiguous, "
        f"{summary.partial} partial, {summary.unavailable} unavailable; "
        f"{summary.writes} local snapshots written"
        f"{' (dry-run)' if dry_run else ''}"
    )
    return 1 if summary.partial or summary.unavailable else 0


def cmd_salesforce_followups(dry_run: bool, smoke: bool) -> int:
    """Run one GET-only Salesforce follow-up check and optional Slack delivery."""
    from slack_sdk import WebClient

    from .slack import salesforce_followups

    channel = primary_channel_id()
    if not channel:
        print("SLACK_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1
    client = None if dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    conn = db.connect_readonly() if dry_run else db.connect()
    outcome = salesforce_followups.run(
        client, channel, conn, dry_run=dry_run, smoke=smoke
    )
    print(f"salesforce follow-ups: {outcome}")
    return 1 if outcome.startswith("unknown:") else 0


def cmd_nudge(dry_run: bool, force: bool = False) -> int:
    """Deliver at most one proactive follow-up about work left unfinished.

    A dry run opens a READ-ONLY connection, so any accidental write raises rather
    than quietly happening — the discipline that `salesforce-sync --dry-run` lacked
    until it was found spending live API calls.
    """
    from slack_sdk import WebClient

    from .slack import nudges

    client = None if dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    conn = db.connect_readonly() if dry_run else db.connect()
    outcome = nudges.run(client, conn, dry_run=dry_run, force=force)
    print(f"nudge: {outcome}")
    return 1 if outcome.startswith("nudge failed") else 0


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
            f"error={row['error'] or 'unknown'}"
        )
    print(f"Slack failures: {len(rows)} pending manual reconciliation", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch. .env is loaded here so every command sees the keys."""
    load_dotenv()
    parser = argparse.ArgumentParser(prog="grant_watch", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_poll = sub.add_parser("poll", help="poll all sources for new leads")
    p_poll.add_argument("--source", help="only run sources whose name contains this")
    p_poll.add_argument(
        "--dry-run", action="store_true", help="poll and grade but write nothing"
    )
    sub.add_parser("seed", help="seed leads from the verified SVPP CSV")
    sub.add_parser("status", help="lead counts by source and grade")
    sub.add_parser("drip-blocked", help="list leads set aside and never delivered")
    p_unblock = sub.add_parser("drip-unblock", help="clear a channel-level drip block")
    p_unblock.add_argument(
        "--channel", default="", help="channel id (defaults to the primary channel)"
    )
    p_drip = sub.add_parser("drip", help="one drip tick (maybe post one nugget)")
    p_drip.add_argument(
        "--force",
        action="store_true",
        help="bypass window/cap/jitter pacing (for testing)",
    )
    p_shadow = sub.add_parser(
        "rich-shadow",
        help="write-free rich-card readiness report (never posts or enriches)",
    )
    p_shadow.add_argument(
        "--limit", type=int, default=100, help="maximum Gold candidates to audit"
    )
    p_prepare = sub.add_parser(
        "rich-prepare",
        help="preview bounded evidence refresh; --execute permits reads + local evidence writes",
    )
    p_prepare.add_argument("--limit", type=int, default=25)
    p_prepare.add_argument(
        "--execute",
        action="store_true",
        help="perform bounded Salesforce/public-source reads and local evidence writes",
    )
    p_prepare.add_argument(
        "--retry-indeterminate",
        action="store_true",
        help="explicitly retry an interrupted possibly-paid call",
    )
    p_nces = sub.add_parser(
        "nces-bind",
        help="bind NCES district identity to Gold leads that lack it (free; --execute to write)",
    )
    p_nces.add_argument(
        "--limit-states",
        type=int,
        default=5,
        help="how many states to fetch this run, most-pending first",
    )
    p_nces.add_argument(
        "--execute",
        action="store_true",
        help="perform the NCES reads and write the matched identities",
    )
    p_shadow.add_argument(
        "--channel-member",
        action="append",
        default=[],
        metavar="SLACK_ID",
        help="offline channel-membership fixture; may be repeated",
    )
    p_drip.add_argument(
        "--dry-run", action="store_true", help="print what would post; write nothing"
    )
    p_retry = sub.add_parser(
        "outreach-retry", help="retry due Persequor handoffs idempotently"
    )
    p_retry.add_argument(
        "--dry-run",
        action="store_true",
        help="count due requests; send and write nothing",
    )
    p_sf = sub.add_parser(
        "salesforce-sync", help="refresh read-only Salesforce lead context"
    )
    p_sf.add_argument(
        "--limit",
        type=int,
        default=25,
        help="maximum leads to check (1-100; default 25)",
    )
    p_sf.add_argument(
        "--dry-run",
        action="store_true",
        help="query Salesforce but write no local snapshots",
    )
    p_nudge = sub.add_parser(
        "nudge", help="follow up on work a rep started and left unfinished"
    )
    p_nudge.add_argument("--dry-run", action="store_true")
    p_nudge.add_argument(
        "--execute",
        action="store_true",
        help="actually post; without it this is a dry run",
    )
    p_nudge.add_argument(
        "--force",
        action="store_true",
        help="skip the business-hours window only; caps and dedup still apply",
    )
    p_followups = sub.add_parser(
        "salesforce-followups", help="check Grant-created Campaign Leads for follow-up"
    )
    p_followups.add_argument(
        "--dry-run",
        action="store_true",
        help="query Salesforce but write or post nothing",
    )
    p_followups.add_argument(
        "--smoke",
        action="store_true",
        help="test a new eligible member without waiting three business days",
    )
    p_failures = sub.add_parser(
        "slack-failures", help="list Slack turns needing manual reconciliation"
    )
    p_failures.add_argument(
        "--mark-reviewed",
        default="",
        metavar="EVENT_ID",
        help="acknowledge one inspected event without replaying it",
    )

    args = parser.parse_args(argv)
    if args.command == "poll":
        return cmd_poll(args.source, args.dry_run)
    if args.command == "seed":
        return cmd_seed()
    if args.command == "drip-blocked":
        return cmd_drip_blocked()
    if args.command == "drip-unblock":
        return cmd_drip_unblock(args.channel)
    if args.command == "drip":
        return cmd_drip(args.force, args.dry_run)
    if args.command == "rich-shadow":
        return cmd_rich_shadow(args.limit, args.channel_member)
    if args.command == "rich-prepare":
        if args.retry_indeterminate and not args.execute:
            parser.error("--retry-indeterminate requires --execute")
        return cmd_rich_prepare(args.limit, args.execute, args.retry_indeterminate)
    if args.command == "nces-bind":
        return cmd_nces_bind(args.limit_states, not args.execute)
    if args.command == "outreach-retry":
        return cmd_outreach_retry(args.dry_run)
    if args.command == "salesforce-sync":
        return cmd_salesforce_sync(args.limit, args.dry_run)
    if args.command == "nudge":
        # Default to a dry run: a command that posts to a team channel should never
        # do so because someone forgot a flag.
        return cmd_nudge(dry_run=not args.execute, force=bool(args.force))
    if args.command == "salesforce-followups":
        return cmd_salesforce_followups(args.dry_run, args.smoke)
    if args.command == "slack-failures":
        return cmd_slack_failures(args.mark_reviewed)
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
