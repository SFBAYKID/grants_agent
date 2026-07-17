---
name: tenant-and-layout
description: Grants tenant identity + where the code, venv, DB, bot and cron live on the droplet
metadata:
  type: reference
---

Verified live 2026-07-14 over the scoped grants SSH (`-i ~/.ssh/grants_droplet -o IdentitiesOnly=yes`, user/host from grants `.env`).

- Tenant Unix user: **grantwatch** (non-sudo; groups `grantwatch`, `users` only — confirmed no sudo/admin group). Home: `/home/grantwatch`. Droplet hostname: `Ubuntu-Monarch-Automation-Server`.
- Repo checkout: `/home/grantwatch/grants_agent`  (see [[deploy-mechanism]] — it is NOT a git repo).
- Venv: `/home/grantwatch/grants_agent/.venv` (Python 3.12.3).
- Local SQLite still in use on droplet: `grant_watch.db` (+ `-wal`/`-shm`) in the repo root — live data, do not clobber. `DATABASE_URL` is set in the droplet `.env` (value not read). Postgres migration (Phase 4) not confirmed active this session.
- Grant Slack bot run command: `.venv/bin/python -u -m grant_watch.slack.grant` (Socket Mode). Logs to `~/grants_agent/bot.log`.
- Bot manager = cron keepalive, NOT systemd. Crontab (grantwatch):
  - `*/5 * * * * ~/grants_agent/run_bot.sh >> ~/grants_agent/cron.log 2>&1`
    (relaunches bot if not running and records a secret-free healthy/restart heartbeat)
  - `0 7 * * 1-5 ... grant_watch.cli poll >> cron.log`
  - `*/30 5-17 * * 1-5 ... grant_watch.cli drip >> cron.log`
  - The broken `salesforce-followups` line (subcommand absent on deployed main) was commented out
    2026-07-17. That single edit was authorized in a specific plan Chase approved in the main
    session; it does NOT create standing permission. Default rule unchanged: the guardian never
    edits crontab unless the operator's prompt for that run explicitly authorizes the exact edit.
    Backup of the pre-edit crontab: `~/crontab.backup.20260717T194112Z`. Recipe that worked:
    backup → sed comment → fail-closed diff/cmp checks (exactly one line changed, keepers
    byte-identical) → `crontab newfile`.
- Persequor intake (verified 2026-07-17): deployed `persequor_client.py` POSTs to
  `PERSEQUOR_API_URL` + `/api/v1/outreach-request` (droplet .env sets the localhost default,
  `http://127.0.0.1:8002`); auth via `X-Persequor-Key`. Read-only liveness check: GET the exact path →
  405 = server up and route present; `/` and `/health` are 404 (no health route). The 8002 listener is
  NOT grantwatch-owned (other tenant's app — never inspect it).
- Keepalive logging was verified from a real cron tick at `2026-07-16T09:15:01Z`:
  `grant_keepalive status=healthy at=2026-07-16T09:15:01Z`. Both `cron.log` and
  `bot.log` are inside `~/grants_agent`. No tenant-owned log-rotation configuration
  existed as of that check; system-wide rotation remains outside guardian scope.
- Healthy-bot signal: `bot.log` tail shows "Grant is listening (Socket Mode)…" + "⚡️ Bolt app is running!"; and `pgrep -f "grant_watch[.]slack[.]grant"` returns a PID.
- Required prod secrets present by NAME (never read values): ANTHROPIC_API_KEY, FIRECRAWL_API_KEY, SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL_ID, plus Salesforce (JWT_KEY_PATH/USERNAME/PASSWORD/SECURITY_TOKEN), SAM_API_KEY, PERSEQUOR_*. Droplet `.env` legitimately DIFFERS from the laptop `.env` (droplet has real Salesforce prod creds; laptop has different Salesforce + Google/Zoom vars) — never sync `.env` laptop→droplet.
