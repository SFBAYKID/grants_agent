---
name: disk-footprint-and-cruft
description: What fills the grants tenant's home on the droplet, the 2026-07-22 snapshot-venv purge that freed 5.88 GiB, and the fact that no log rotation exists
metadata:
  type: reference
---

Measured read-only 2026-07-22 over the scoped grants SSH. Droplet root was at **97% (45.8 GiB used of
47.4 GiB, 1.6 GiB free)**; **inodes only 31% used** — so a "disk full" on this box is a BLOCK problem,
never an inode problem. Always run `df -i /` alongside `df -h /` before diagnosing.

**The grants tenant is NOT the cause of the 97%.** `/home/grantwatch` was **7.70 GiB**, i.e. ~17% of used
space. The other ~38 GiB lives outside the tenant (other tenants / system) and is out of guardian scope.

## RESOLVED 2026-07-22: snapshot venvs purged — home is now 1.9 GiB

Chase authorized deleting the `.venv` inside the **26 oldest** `.grants_agent.previous.pre-*` snapshots
(the snapshot DIRS and all their non-venv contents were preserved; the 2 newest snapshots and the live
venv were untouched). Result, all verified: home **7.49 GB → 1.83 GB apparent**; root `/` **95% → 83%**,
used 48.10 GB → 41.79 GB, avail 2.76 GB → 9.08 GB (all four from `df -B1`, exact bytes);
**6,312,767,488 allocated bytes reclaimed** (0.015% under the 6,313,738,240 predicted — the gap is
concurrent shared-box writes, not an error). Inodes 1,942,178 → 1,695,318 (−246,860). Bot PID and
crontab sha unchanged. The numbers further down describe the PRE-purge state.

**FOUR virtualenvs remain on the box** (an earlier version of this note said three, which was wrong —
it omitted the staging one):
1. the LIVE `~/grants_agent/.venv`;
2. + 3. the two retained snapshots' `.venv` — `.grants_agent.previous.pre-264e0a7b…` and
   `.grants_agent.previous.pre-bdea1cd3…`, kept complete and runnable as dependency rollback;
4. `.deploy_staging/b0b87e0_20260715T092424Z/checks_venv` — untouched because `.deploy_staging` was
   outside the authorized scope.

**State units explicitly; the three tools do not measure the same thing.**
- `du -sb` reports APPARENT size in decimal bytes (sum of file lengths). For the purge: home
  7.49 GB → 1.83 GB apparent, a 5.66 GB apparent delta.
- `du -s --block-size=1` reports ALLOCATED bytes (blocks actually occupied). For these venv trees that
  was 6,313,738,240 — and it is the figure that matched the change `df` observed.
- `df -h` reports ROUNDED filesystem figures (95% → 83%); use `df -B1` when a number must be exact.

For THESE trees — roughly 247k small files, most far under one block — apparent undercounted allocated
by about 11%. That ratio is a property of this file-size distribution, **not a universal filesystem
rule**: a tree of large files would show almost no gap, and a sparse file can invert it. The durable
lesson is narrower: predict a reclaim with `du --block-size=1`, because allocated space is what `df`
frees; `du -sb` will under-predict wherever small files dominate.

**Safe-purge recipe (reusable):** literal path list in a bash array — no globs, no `find -delete`; per
path re-assert suffix `*/.venv` + prefix + `test -d` + `! test -L` + not-a-KEEP/LIVE path, capture
`parent_entries` before/after and require the delta to be exactly 1 with the parent still a directory,
and abort the whole loop if a parent ever disappears. Prove nothing is in use first by grepping every
grantwatch `/proc/<pid>/maps` for the doomed prefix (and re-check `(deleted)` map entries after).
GOTCHA: do NOT write `grep -c … || echo 0`. `grep -c` ALREADY prints `0` when it matches nothing, and
it also exits 1 — so the `||` fires and appends a second `0`, producing the two-line string `0\n0`.
That breaks any numeric comparison and reads as a false positive. Use `grep … | wc -l`.

## Intentional debt left in place 2026-07-22 (each deliberate, none forgotten)

- **`.deploy_staging` (~287 MB allocated) untouched** — outside the authorized scope; still holds the
  fourth virtualenv (`checks_venv`).
- **23 historical production `.env` copies untouched**, inside the retained snapshot directories. They
  hold Slack, Anthropic and Salesforce PRODUCTION credentials as of 2026-07-14→16. Permission-contained
  today (home `700`, snapshot dirs `700`, files `600`), so this is exposure surface, not an incident —
  but deleting the copies would NOT remediate past exposure. Rotation is the remedy, and it is a
  separate decision that has not been taken.
- **`~/.cache/pip` (~70 MB) untouched** — outside the authorized scope.

## Where the 7.7 GiB went (pre-purge) — 86% of it was duplicated virtualenvs

- **29 separate `.venv` directories under the home, 6.6 GiB total.** The live one
  (`~/grants_agent/.venv`) is 241 MB; the other 28 were copies inside deploy snapshots/staging.
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
