"""Operational CLI commands: repair, discovery, and buying contacts.

Split out of `cli.py` when it crossed the 1,000-line cap (CLAUDE.md rule 4). The
boundary is real rather than arbitrary: everything here is a job an OPERATOR runs or
cron fires against live systems — repairing dead conversations, reading Slack for
unmet asks, spending money on contacts. They change when production behaviour needs
to change, while the commands left behind in `cli.py` are the data pipeline.
"""

from __future__ import annotations

from . import db


def cmd_fill_contacts(
    campaign: str, limit: int, max_credits: int, dry_run: bool
) -> int:
    """Buy decision-maker contacts for leads that have none, within a credit ceiling.

    The gap this closes: buying a contact could only happen one lead at a time through
    a Slack conversation, so 997 of 1000 purchased credits sat unused while 9 of 13
    Leads on a live campaign held nothing but a name and a state.
    """
    from . import contact_fill

    conn = db.connect_readonly() if dry_run else db.connect()
    if campaign:
        rows = conn.execute(
            """SELECT DISTINCT lead_id FROM crm_action_items
                WHERE campaign_name=? AND lead_id IS NOT NULL
                ORDER BY lead_id LIMIT ?""",
            (campaign, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id AS lead_id FROM leads
                WHERE lead_grade='gold' ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    lead_ids = [int(r["lead_id"]) for r in rows]
    if not lead_ids:
        print("fill-contacts: no leads matched")
        return 0
    print(
        f"fill-contacts: {len(lead_ids)} lead(s), "
        f"{contact_fill.remaining_credits(conn)} credit(s) left this period"
    )
    outcome = contact_fill.fill_contacts(
        conn,
        lead_ids,
        max_credits=max_credits,
        dry_run=dry_run,
        requested_by="cli",
    )
    prefix = "[dry-run] " if dry_run else ""
    print(prefix + outcome.summary())
    return 0


def cmd_watchdog(dry_run: bool) -> int:
    """Repair conversations that died mid-turn and left a spinner on screen.

    Runs between restarts, because a turn killed at 18:42 should not wait for the
    next deploy to be resolved — Chase watched one sit on "Thinking…" for four hours.
    """
    import os

    from slack_sdk import WebClient

    from .slack import watchdog

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("watchdog: SLACK_BOT_TOKEN is not set")
        return 1
    client = WebClient(token=token)
    conn = db.connect_readonly() if dry_run else db.connect()
    print(
        watchdog.run(
            client,
            conn,
            bot_id=str(client.auth_test().get("user_id") or ""),
            dry_run=dry_run,
        )
    )
    return 0


def cmd_scan_threads(channel: str, dry_run: bool) -> int:
    """Read a channel's recent conversations and record what went unanswered.

    The standing replacement for hand-seeding asks out of a JSON file somebody wrote
    after reading July's transcripts by eye. This runs weekly and finds tomorrow's.
    """
    import os

    from anthropic import Anthropic
    from slack_sdk import WebClient

    from . import thread_scanner

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("scan-threads: SLACK_BOT_TOKEN is not set")
        return 1
    client = Anthropic()

    def ask_model(prompt: str) -> str:
        """One cheap classification pass over a single thread."""
        reply = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text
            for block in reply.content
            if getattr(block, "type", "") == "text"
        )

    slack = WebClient(token=token)
    # Ask Slack who Grant is rather than hard-coding an id: the scan must ignore
    # threads belonging to the other bots that share this channel.
    identity = slack.auth_test()
    conn = db.connect_readonly() if dry_run else db.connect()
    print(
        thread_scanner.scan_channel(
            slack,
            conn,
            channel,
            ask_model,
            dry_run=dry_run,
            grant_user=str(identity.get("user_id") or ""),
            grant_bot=str(identity.get("bot_id") or ""),
        )
    )
    return 0
