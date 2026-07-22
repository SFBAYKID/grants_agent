---
name: disk-footprint-and-cruft
description: What actually fills the grants tenant's home on the droplet (29 duplicated .venv copies = 86%), what is safe cruft, and the fact that no log rotation exists
metadata:
  type: reference
---

Measured read-only 2026-07-22 over the scoped grants SSH. Droplet root was at **97% (45.8 GiB used of
47.4 GiB, 1.6 GiB free)**; **inodes only 31% used** — so a "disk full" on this box is a BLOCK problem,
never an inode problem. Always run `df -i /` alongside `df -h /` before diagnosing.

**The grants tenant is NOT the cause of the 97%.** `/home/grantwatch` = **7.70 GiB**, i.e. ~17% of used
space. The other ~38 GiB lives outside the tenant (other tenants / system) and is out of guardian scope.

## Where the 7.7 GiB goes — 86% of it is duplicated virtualenvs

- **29 separate `.venv` directories under the home, 6.6 GiB total.** The live one
  (`~/grants_agent/.venv`) is 241 MB; the other 28 are copies inside deploy snapshots/staging.
- `.grants_agent.previous.pre-<sha>` — **28 full-tree deploy snapshots, 7.0 GiB**, dated 2026-07-14 →
  2026-07-16 only. Each is ~265 MB of which 241 MB is a duplicate `.venv`, plus a frozen ~20 MB
  `grant_watch.db` and a frozen 1.2 MB `cron.log`. The mechanism that made them was **superseded around
  2026-07-16** by the small `pre-<sha>-overwritten-files.<UTC>.tar.gz` approach (the 2026-07-22 deploy of
  `264b0e2` produced a 30 KB tarball, not a snapshot) — so these accumulate only historically, they are
  not being added to. Watch for a duplicate short-sha/long-sha PAIR of the same commit
  (`pre-3f35a269` and `pre-3f35a2697450…`) — one is pure redundancy.
- `.deploy_staging/` 274 MB — 12 dirs; only `b0b87e0_20260715T092424Z` is big (273 MB, another `.venv`).
- `.cache/pip` 67 MB. `backups/` 13 MB + `.grants_backups/` 5.4 MB + `grants_agent_ops_backups/` 20 MB
  (all 2026-07-14→16 era, DB copies from when the DB was <1 MB).
- Top-level `grant_watch.db.bak.<UTC>` set: 6 backups + `-wal`/`-shm` companions = 130 MB.
- Live repo `~/grants_agent` = 253 MB total, of which `.venv` is 241 MB — the actual code+data is ~12 MB.
- `__pycache__`: 11,177 dirs / 1.4 GiB home-wide, but that is a SUBSET of the `.venv` totals, not
  additive. Live repo's own `__pycache__` is 47 MB.
- Leaked temp dirs: ~15 `/tmp/grant_xlsx_*` dirs from the xlsx export path (2026-07-13/14) were never
  cleaned up. Tiny (4 KB each) but it is a real leak in that code path.

## No log rotation exists — logs are append-only forever

`crontab -l` redirects everything with `>>` into `~/grants_agent/cron.log`; `run_bot.sh` contains no
rotate/truncate logic; the tenant owns no logrotate config (no `~/.config/logrotate*`, nothing matching
`*logrotate*` in the home). `cron.log` birth 2026-07-14, 1.36 MB / 11,689 lines after 8 days ≈ 150 KB/day
(~55 MB/yr) — not a space threat today, but uncapped. `bot.log` 17.5 KB (it logs almost nothing — see
[[grant-bot-silent-llm-fallback]]).

## Backup-deletion rule

A `.db` backup is NOT self-contained without its `-wal`. Example: `grant_watch.db.bak.20260722T175509Z`
carries a 4.1 MB `-wal`. Always treat `.db` + `-wal` + `-shm` as one unit when keeping OR deleting —
same rule as [[tenant-db-write-safety]]. Also: `backups/env_before_*` and `~/.env.bak.*` hold SECRET
VALUES at mode 600 — never print them; removing old copies is a security improvement, not just cleanup.
See [[deploy-mechanism]] for how the snapshots got created.
