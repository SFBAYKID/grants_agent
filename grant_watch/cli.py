"""Command-line entrypoints for grant_watch.

Usage (from the repo root, venv active):
    python -m grant_watch.cli poll [--source NAME] [--dry-run]
    python -m grant_watch.cli seed
    python -m grant_watch.cli status

--dry-run polls and grades but writes NOTHING (no DB rows, no run log) — required by
CLAUDE.md for anything that will later feed Slack. Errors in one source never abort
the others; they are printed and recorded in the `runs` table.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import db, scoring
from .models import RunStats
from .sources import POLLERS, sam_gov

SEED_CSV = Path(__file__).resolve().parent.parent / "data" / "svpp_active_awards_CA_MI_PA_WA.csv"


def _active_pollers() -> list[tuple[str, object]]:
    """The static registry plus SAM.gov when its key is configured."""
    pollers = list(POLLERS)
    sam_key = os.environ.get("SAM_API_KEY", "")
    if sam_key:
        pollers.append(("SAM.gov", lambda: sam_gov.poll(sam_key)))
    else:
        print("[skip] SAM.gov — set SAM_API_KEY in .env to enable", file=sys.stderr)
    return pollers


def cmd_poll(only_source: str | None, dry_run: bool) -> int:
    """Run all (or one) pollers, grade items, upsert new leads, log the run."""
    conn = db.connect()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_total = 0

    for name, poll_fn in _active_pollers():
        if only_source and only_source.lower() not in name.lower():
            continue
        stats = RunStats(source=name)
        try:
            items = poll_fn()  # type: ignore[operator]
            stats.items_seen = len(items)
            for item in items:
                lead = scoring.grade(item)
                if dry_run:
                    continue
                if db.upsert_lead(conn, lead):
                    stats.items_new += 1
                    fresh = " [FRESH]" if scoring.is_fresh(item) else ""
                    amt = f" ${item.amount:,.0f}" if item.amount else ""
                    print(f"  NEW {lead.grade.value.upper():6s} [{item.source}] "
                          f"{item.entity}{amt} — {item.title[:70]}{fresh}")
        except Exception as exc:  # one bad source must not kill the run
            stats.errors = f"{type(exc).__name__}: {exc}"
            print(f"[{name}] ERROR: {stats.errors}", file=sys.stderr)
        if not dry_run:
            db.log_run(conn, started, stats)
        new_total += stats.items_new
        print(f"[{name}] {stats.items_seen} items, {stats.items_new} new"
              f"{' (dry-run: nothing written)' if dry_run else ''}")
    return 0 if new_total >= 0 else 1


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

    args = parser.parse_args(argv)
    if args.command == "poll":
        return cmd_poll(args.source, args.dry_run)
    if args.command == "seed":
        return cmd_seed()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
