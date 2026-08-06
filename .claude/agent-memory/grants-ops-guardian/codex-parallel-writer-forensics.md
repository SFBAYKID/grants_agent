---
name: codex-parallel-writer-forensics
description: How to tell a Codex/other-tool deploy rehearsal apart from tampering on the grantwatch tenant — its on-disk signatures, why keepalive gaps appear, and the read-only recipe that establishes ground truth
metadata:
  type: project
---

A second toolchain (OpenAI Codex — ChatGPT desktop + `@openai/codex` CLI) has operated the
`grantwatch` tenant in parallel with this repo's guardian. Its footprints are well-formed and
reversible, NOT tampering. Learn its signatures before concluding "rogue writer".

**Why:** on 2026-07-24/25 the `*/5` watchdog cron line was seen PRESENT at 18:56:34 PT and ABSENT at
19:11:51 PT, and each tool read the other's edits as hostile. A read-only audit on 2026-07-25 showed
every one of those transitions was a deliberate stop→work→restore cycle, and the crontab ended
byte-identical to the healthy 4-line form (`sha256 6275d502…`).

**How to apply:** when droplet state looks "wrong", check these before escalating.

## Signatures of the other writer (all inside the grants tenant, all reversible)
- `~/.deploy_staging/<label>-<rev>.<TS>/` — `deploy_src/` (the staged tree), `source.tar.gz`,
  `rsync-dry-run.txt`, `crontab.paused`. A `deploy_src/.codex` directory identifies the toolchain.
- `~/backups/deploy-<rev>-<TS>/` — `code_before/`, `crontab.before`, `crontab.restored.check`,
  `deployed_revision.before`, `grant_watch.db.before` (+`-wal`/`-shm`), `pre_migration_state.json`,
  `predeploy-hashes.txt`, `listener.before`, `bot-log-before.txt`, `restore-start*.txt`,
  and `rehearsal-migrations-N-M.db` — migrations rehearsed on a **copy**, never the live DB.
- `~/.grant_listener_handoff/` — crontab PAIRS: 470 bytes WITH the keepalive line, 400 bytes
  WITHOUT it. It removes `*/5` so the watchdog cannot resurrect Grant mid-operation, then restores.
- `~/crontab.backup.pre-grant-stop.*` / `crontab.backup.pre-exclusive-handoff.*`.

## Keepalive gaps are the fingerprint, not the damage
`run_bot.sh` logs `status=healthy` when the bot is up and `status=restart_attempt` when it restarts
it. A tick that logs **nothing at all** means the `*/5` line was absent from the crontab at that
instant — i.e. someone was mid stop/restore, not that cron died. A missing tick paired with a
`restore-start.txt` timestamp matching the bot's start time is a completed, clean cycle.

## Read-only ground-truth recipe (no writes, no process control)
```bash
crontab -l | cat -An; crontab -l | sha256sum        # 6275d502… = healthy 4-line
pgrep -af 'grant_watch[.]slack[.]grant'             # expect exactly 1, PPID 1
grep -a grant_keepalive cron.log | sed -n 's/.*at=\(...\)Z.*/\1/p' | awk '...gap>360s...'
grep -ac 'status=restart_attempt' cron.log          # silent tick != restart
```
Gap analysis + per-hour tick counts (`expect 12/hr`) localize every interruption to the minute.
Live DB must be opened `file:grant_watch.db?mode=ro` + `PRAGMA query_only=ON` — a normal
`db.connect()` self-applies migrations. See [[tenant-db-write-safety]] and [[deploy-mechanism]].

## Divergence to watch: origin is AHEAD of this clone
`origin/review/rich-award-card-campaign-20260723` was at **359c1e3** while this repo's HEAD was
**e8ecf0c** — the other tool pushed commits this clone has never fetched, adding migrations **27–28**
and files (`grant_watch/migration_runner.py`, `enrich/salesforce_campaign_batch.py`,
`slack/salesforce_actions.py`) absent from the live tree. Before reasoning about "the" branch state,
run `git ls-remote --heads origin` — do not assume local HEAD is the tip.
Related: [[rich-card-deploy-e8ecf0c]], [[migration-version-collision]].
