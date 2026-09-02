"""Operational CLI commands: repair, discovery, and buying contacts.

Split out of `cli.py` when it crossed the 1,000-line cap (CLAUDE.md rule 4). The
boundary is real rather than arbitrary: everything here is a job an OPERATOR runs or
cron fires against live systems — repairing dead conversations, reading Slack for
unmet asks, spending money on contacts. They change when production behaviour needs
to change, while the commands left behind in `cli.py` are the data pipeline.
"""

from __future__ import annotations

from . import db
from .llm import anthropic_client_options


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
        # RESOLVED THROUGH SALESFORCE, because `crm_action_items` has no campaign
        # column at all — the first version of this query named one and raised
        # `no such column`. Worse than the crash was the shape of the recovery: the
        # `else` branch below is NOT a fallback, it selects the 25 newest gold leads,
        # so an operator who reacted by dropping the flag would have quietly bought
        # contacts for a completely different set of leads.
        from .enrich.salesforce_campaign_gateway import SalesforceCampaignGateway

        gateway = SalesforceCampaignGateway()
        found = gateway.search_campaigns(campaign)
        if not found:
            print(f"fill-contacts: no Salesforce campaign named {campaign!r}")
            return 1
        member_ids = [
            str(r["LeadId"])
            for r in gateway._query_all(
                "SELECT LeadId FROM CampaignMember "
                f"WHERE CampaignId='{found[0].record_id}' AND LeadId != null"
            )
        ]
        if not member_ids:
            print(f"fill-contacts: campaign {campaign!r} has no Lead members")
            return 1
        quoted = ",".join("?" for _ in member_ids)
        rows = conn.execute(
            f"""SELECT DISTINCT lead_id FROM crm_action_items
                 WHERE salesforce_id IN ({quoted}) AND lead_id IS NOT NULL
                 ORDER BY lead_id LIMIT ?""",
            (*member_ids, limit),
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

    # HOUSEKEEPING RIDES THE ONLY JOB THAT RUNS AROUND THE CLOCK, rather than earning
    # its own cron line. `user_memory.purge` had NO caller anywhere outside tests, so
    # six-month expiry was enforced only by `recall`'s filter — correct for what a
    # person sees, and it left every lapsed row on disk forever. A cheap indexed
    # DELETE is a fair passenger on a tick that already opens the database.
    if not dry_run:
        from . import user_memory

        dropped = user_memory.purge(conn)
        if dropped:
            print(f"memory: purged {dropped} expired item(s)")
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
    client = Anthropic(**anthropic_client_options())

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


def cmd_daily_list(limit: int, force: bool, dry_run: bool) -> int:
    """Post the day's freshest-awards list to the primary channel.

    Designed for ONE cron tick a day. The one-a-day cap lives in the ledger rather
    than in cron, so a double tick, a retry, or a manual run cannot produce a second
    list — the same reason `posts` gates the drip rather than the crontab doing it.
    """
    import os
    import sys

    from slack_sdk import WebClient

    from .config import primary_channel_id
    from .slack import daily_list

    channel = primary_channel_id()
    if not channel:
        print("SLACK_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1
    client = None if dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    conn = db.connect_readonly() if dry_run else db.connect()
    try:
        outcome = daily_list.run(
            client, channel, conn, limit=limit, force=force, dry_run=dry_run
        )
    finally:
        conn.close()
    print(outcome)
    # A refusal or an ambiguous send must exit non-zero: cron.log is the only alarm
    # this system has, and a silent zero is how a dead feature stays dead.
    return 1 if outcome.startswith(("error", "unknown")) else 0


def cmd_drip_unblock(channel: str) -> int:
    """Clear a channel-level block after an operator has fixed Slack.

    A systemic Slack failure (`channel_not_found`, `invalid_auth`, …) blocks the channel
    for an escalating 1h-8h period, because retrying every 30 minutes cannot help and
    used to consume a lead each time. The guard expires on its own; this command only
    resumes SOONER, once an operator knows Slack is fixed."""
    import sys

    from .config import primary_channel_id

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
