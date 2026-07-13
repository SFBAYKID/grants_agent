"""grant_watch — weekly security-grant/RFP lead watcher for Monarch Connected.

Package layout (see architectural.md):
    models.py    typed data models shared across the pipeline
    db.py        SQLite storage: 4-table schema, dedup upserts, run logging, CSV seed
    scoring.py   GOLD / SILVER / watch grading + freshness rules
    sources/     one module per data source, each with an honest VERIFICATION label
    cli.py       command-line entrypoints (poll / seed / status), --dry-run aware
"""

__version__ = "0.2.0"
