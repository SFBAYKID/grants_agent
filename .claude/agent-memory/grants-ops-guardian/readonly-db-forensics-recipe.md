---
name: readonly-db-forensics-recipe
description: How to query the live production SQLite with genuinely ZERO writes (mode=ro works against the hot WAL), plus the current crontab's 10 lines characterized and the OregonBuys poller that 404s every run
metadata:
  type: reference
---

**Zero-write query recipe (proven 2026-08-09).** `db.connect()` MIGRATES, so never use it for
forensics. Instead:

```python
sqlite3.connect("file:/home/grantwatch/grants_agent/grant_watch.db?mode=ro", uri=True, timeout=15)
```

This works against the LIVE hot WAL (10 MB uncheckpointed at the time) because the bot holds the
`-shm` open, so a read-only connection can attach and see uncommitted-to-main data. No
`VACUUM INTO` scratch copy is needed, and no file is created anywhere. WAL readers never block the
writer. Ship the analysis script over ssh stdin as a heredoc (`.venv/bin/python - <<'PYEOF'`) inside
a `bash -s` payload so nothing is written to the tenant's disk at all.

**Table-name gotcha:** there is NO `conversation_sessions` table. Conversations live in
`slack_conversation_threads` (workspace, channel, thread_ts, initiated_by, created_at,
last_active_at) plus `slack_event_receipts` (per-event, with state/action_state/delivery_state).
38 tables total at schema 28.

**Crontab, characterized 2026-08-09** — 10 lines = **5 active jobs + 5 comment lines**, sha256
`575fbc7c…0041a72`. All times Pacific (server TZ America/Los_Angeles).
| when (PT) | job |
|---|---|
| `*/5 * * * *` | `run_bot.sh` keepalive — relaunches the bot, writes `grant_keepalive status=` |
| `*/30 4-17 * * 1-5` | `cli drip` — the one daily card; posts inside the 10:30–11:00 slot |
| `0 7 * * 1-5` | `cli poll` — 7 sources, takes 9–9.5 min |
| `45 7 * * 1-5` | `cli rich-prepare --execute` — **PAID** (Firecrawl contact discovery) + read-only Salesforce |
| `20 7 * * 1` | `cli nces-bind --limit-states 12 --execute` — weekly Monday, free/keyless |

The 5-line→10-line "drift" from the 2026-08-06 baseline is fully explained by the `nces-bind` line
plus its 4-line justification comment. Nothing unaccounted for, no duplicates, every referenced path
exists, and `salesforce-followups` stays commented out. NOTE its comment ("subcommand absent on
deployed main") is now STALE — `salesforce-followups` IS a valid subcommand at 90f0420; it must stay
disabled for the reason in CLAUDE.md M1 (it arbitrates only via slot rows legacy `pacing_ok` cannot
see), not because it is missing. `nces-bind` has never actually run (added after Mon 08-03; next
fire Mon 08-10).

**Standing log facts:** no rotation on either log. `cron.log` 1.8 MB (7009 healthy keepalives, 37
restarts). `bot.log` 63 KB but it is APPENDED across restarts (118 "Grant is listening" banners) and
carries a `[tool-turn N] <tool>:<args>` trace — the only per-conversation tool record that exists.
It also carries `[tool-error]` + real tracebacks, contradicting the older belief that bot.log logs
nothing on tool failure ([[grant-bot-silent-llm-fallback]] applies to LLM failures, not tool ones).

**Broken source:** `[OregonBuys recent bids] ERROR: HTTPError: 404` on
`https://www.oregon.gov/das/ORBuys/Documents/Recent-Bids.pdf` — 11 occurrences, every recent poll.
The PDF moved or was removed; the poller has returned `0 items` since. Only other poll error ever:
one `USASpending SVPP ReadTimeout`.
