"""grant_watch — weekly security-grant/RFP lead watcher for Monarch Connected.

Package layout (see architectural.md):
    models.py         typed source, funding-event, lead, and run models
    migrations.py     ordered SQLite schema and workflow-state migrations
    db.py             SQLite repositories, deduplication, and evidence persistence
    scoring.py        GOLD / SILVER / watch grading and freshness rules
    source_catalog.py nationwide source discovery evidence and generated reports
    sources/          one poller module per integrated official source
    enrich/           public contact, NCES, and read-only/gated CRM workflows
    slack/            channel-only conversation, search, drip, and handoff workflows
    cli.py            dry-run-aware operational entrypoints
"""

__version__ = "0.2.0"
