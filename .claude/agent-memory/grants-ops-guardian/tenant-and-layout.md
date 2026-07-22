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
- **Slack channel(s): `SLACK_CHANNEL_ID` in `.env`.** MULTI-CHANNEL since 2026-07-20 (commit `a1d2484`):
  value is a comma-separated list `C01DGT9D11D,C0B02721MNK` = production `monarch-cloud-team-vekada`
  (FIRST = PRIMARY: drip posts + orphan-spinner sweep target it) + `C0B02721MNK` playground (also a valid
  channel for answering mentions). Parsing lives in `grant_watch/config.py`
  (`configured_channel_ids()` splits on comma; `primary_channel_id()` = first). CRITICAL ordering
  gotcha: pre-a1d2484 code read `SLACK_CHANNEL_ID` as ONE literal id — setting the comma value with old
  code makes Grant deaf in ALL channels. So a multi-channel `.env` change is only safe AFTER a1d2484 is
  deployed; sequence = rsync new code → edit `.env` → restart (the running old bot doesn't re-read `.env`
  until restart, so order-within is fine as long as the code is on disk before the restart). Bot reads
  the value ONLY at startup, so a channel change needs a restart. Channel IDs are non-secret. Slack-side
  caveat (can't verify read-only from the box without the bot token): the Grant app must be a MEMBER of
  each channel to post/read; `sweep_orphaned_spinners` swallows a not-in-channel error silently, so a
  clean bot.log does NOT prove membership — confirm on the Slack side.
- Bot manager = cron keepalive, NOT systemd. Server TZ = **America/Los_Angeles (PDT/PT)**, NTP-synced
  (verified `timedatectl` 2026-07-19) — so ALL cron times are Pacific. Crontab (grantwatch), 4 lines:
  - `*/5 * * * * ~/grants_agent/run_bot.sh >> ~/grants_agent/cron.log 2>&1`
    (relaunches bot if not running and records a secret-free healthy/restart heartbeat)
  - `0 7 * * 1-5 ... grant_watch.cli poll >> cron.log`  (07:00 PT = 10:00 ET, weekdays)
  - `*/30 4-17 * * 1-5 ... grant_watch.cli drip >> cron.log`  (04:00–17:30 PT, weekdays)
  - The broken `salesforce-followups` line (subcommand absent on deployed main) was commented out
    2026-07-17. That single edit was authorized in a specific plan Chase approved in the main
    session; it does NOT create standing permission. Default rule unchanged: the guardian never
    edits crontab unless the operator's prompt for that run explicitly authorizes the exact edit.
    NOTE the disabled comment line ALSO contains `*/30 5-17` — a global `sed s/5-17/4-17/` would
    corrupt it; always target the ACTIVE drip line (anchor `^\*/30 5-17` + require `grant_watch.cli
    drip`; the `#` comment can't match `^`).
  - **Drip window moved 05:00 PT → 04:00 PT (= 7am ET) on 2026-07-19**, operator-authorized that run,
    to match Chase's intended window "7am ET through 5pm PT" once app-side drip window opened at 7am ET
    in commit ba0a7b7. Only the drip line's hour field changed (`5-17`→`4-17`); poll/keepalive/disabled
    comment byte-identical. Backups: `~/crontab.backup.20260720T002831Z` (pre this edit) and the older
    `~/crontab.backup.20260717T194112Z`. Recipe that worked: `crontab -l` → `cp -a` backup → `awk` sub
    on the anchored active drip line → fail-closed guards (line count stable, exactly ONE line changed,
    removed==5-17-drip / added==4-17-drip, poll+keepalive+disabled `grep -qxF` byte-identical, no active
    line still carries 5-17) → `crontab newfile` → read-back diff == new. **CAP CORRECTION (2026-07-20):
    the old note here said "cap is 3/day" — that is STALE. `DAILY_CAP = 1` since commit 194d364
    (2026-07-18, "one best card a day"). The `(N)` in `drip: skip: daily cap reached (N)` is the CAP
    CONSTANT, not the day's post count. See [[drip-pacing-and-cap]].**
  - **GOTCHA (cost one round-trip 2026-07-19): `set -o pipefail` + `diff` as pipeline stage-1.**
    `diff a b | grep '^<' | grep -q PATTERN` ALWAYS returns non-zero under pipefail because `diff`
    exits 1 when files differ — regardless of whether grep matched — so a `|| exit 1` guard trips on
    a correct change. Fix: capture `DIFFOUT=$(diff a b || true)` once, then `printf '%s\n' "$DIFFOUT"
    | grep …`. (macOS test with `printf|grep` won't reproduce it — printf exits 0.)
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
- Security-RFP feature flag: `RFP_DISCOVERY_ENABLED=1` was APPENDED to droplet `.env` on 2026-07-18
  (Chase-authorized, benign non-secret flag). With it set, `_active_pollers()` builds 7 pollers incl.
  **"Security RFP discovery"** (without it the poller is skipped, name absent). Read-only proof (no paid
  poll): `cd ~/grants_agent && .venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); from
  grant_watch.cli import _active_pollers; print([n for n,_ in _active_pollers()])"` → list includes it.
  Safe append-only `.env` edit recipe (secret-safe, reusable): capture pre `wc -c`+sha256 → `cp -a` a
  mode-600 backup (`~/.env.bak.<UTCstamp>`) → guard trailing newline → `printf 'KEY=1\n' >> .env` (value
  non-secret, fine in history) → verify `wc -l` delta==1, `grep -c '^KEY=1'`==1, `grep -c '^KEY='`==1
  (no dup), and PRE-IMAGE proof `head -c <presize> .env | sha256` == pre sha (proves existing bytes
  untouched), plus a `cut -d= -f1` key-NAME diff showing exactly one added line. Never print values.
- Drip slot band (added to droplet `.env` 2026-07-22, Chase-authorized, NON-secret):
  `DRIP_SLOT_START_PT=10:30`, `DRIP_SLOT_END_PT=11:00` — the Pacific band the single daily card
  may land in (read by `drip.slot_band()`; unset falls back to the code defaults 10:00–11:30).
  Appended with the same secret-safe append-only recipe as `RFP_DISCOVERY_ENABLED` above
  (pre-image `head -c <presize> | sha256` proof + key-NAME diff + dup guard). Changing the band is
  an `.env`-only edit — **no deploy and no crontab change**, and the bot/cron re-read `.env` per
  process, though the long-lived bot needs a restart. Beware the tick-quantization trap in
  [[drip-slot-band-vs-cron-granularity]]. `GRANT_TERRITORY_OWNERS` is NOT set, so
  `territory.DEFAULT_TERRITORY_OWNERS` (CA/OR/PA/TX/WA only) applies — measured 2026-07-22,
  369 of the 544 gold candidates sit in a mapped state and **175 would post with no @mention**
  (by design: `territory.py` never guesses a Slack id). Adding states is an `.env` edit too.
- Salesforce SANDBOX-write config on droplet `.env` (as of 2026-07-17): `SALESFORCE_CAMPAIGN_WRITES_ENABLED=1`, `SALESFORCE_WRITE_EXPECT_SANDBOX=1`, and `SALESFORCE_WRITE_ORG_ID=<18-char 00D… org id>` (value never logged). The last two were added 2026-07-17 by surgical two-key copy from the laptop `.env` — Chase-approved for that specific run. This did NOT change the "never sync `.env`" rule; any future Salesforce-config change still needs Chase's explicit per-run approval. Secret-safe edit recipe that worked: deliver the value into a mode-600 `~/grants_agent/.orgval.tmp` via ssh STDIN (never argv), `awk` reads it via `getline` into a mode-600 temp `.env`, guard on `diff added==N removed==0` + key counts before atomic `mv`, EXIT-trap scrubs the temp, backup at `~/.env.bak.<UTCstamp>` (cp -a, 600). Verify the LIVE bot loaded it with `tr '\0' '\n' < /proc/<pid>/environ | grep -Ec '^KEY=..*'` (count only).
