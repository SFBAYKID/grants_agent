---
name: deploy-5f09200-fallback-routing
description: 359c1e3→5f09200 deployed 2026-08-06 07:10-07:15Z no-restart (PID 633555 held); AST-diff beats name-status for "no migration" claims; --files-from surgical rsync from archive staging; cutoff now 11:30 + cutoff-miss falls back to daily
metadata:
  type: project
---

**2026-08-06 production deploy 359c1e3 → `5f09200afab8e7f14cc8b09a9618d0c605cb8aed` (rich→daily
fallback + routing revision), all verified, ~7 min, NO bot restart, NO outage.** Chase-authorized.
Artifact: full-tree `git archive` of 5f09200, sha256 `ecfce5888672fe20eb472641ffe9e5a4c964e7f73181f8a95b485ddd2bcde85d`,
911 files, 0 symlinks, `diff -r`-identical to the commit tree, no .env/.git/db/secrets. Local pytest
gate at HEAD first: 992 passed / 74 skipped. Deployable delta = 46 files (41 mods + 5 adds:
slack/drip_card.py, tests/drip_support.py, test_drip_builders.py, test_drip_card.py,
test_salesforce_slack_action_paths.py); all 46 droplet sha256 == 5f09200 blobs; `.env` sha
`b3f338ff…c3bff`, crontab 5 lines `70e309aa…876f`, db set byte-sizes, run_bot.sh all unchanged.
Listener PID 633555 held throughout. Drip dry-run after: `drip: skip: waiting for today's 10:41
Pacific slot` (rich path, exit 0).

**Claim-sharpening lessons (why this file exists):**
- "NO migration files change" was FALSE by `git diff --name-status` (migrations_rich.py appears) but
  TRUE semantically: **compare `ast.dump()` of both blobs**, not the text diff — the whole 4a4d550
  ruff-format commit made 14 grant_watch files text-diff but AST-identical. AST comparison is the
  right tool for "is this deploy-inert formatting"; a `-w` diff is not (line rewraps still differ).
- "The bot imports none of the changed modules" was overbroad: `territory` IS lazily bot-reachable
  (grant.py → `campaign.snapshot` → `routing` → `territory`). Safe anyway because no bot-side module
  CALLS a territory function (only cron-side slack/drip.py does) and `routing_line`'s signature is
  unchanged. The check that settles it: import the bot's full lazy closure in the laptop venv and
  intersect `sys.modules` with the semantic-change set — grep alone missed this.
- A `-def _ambiguous(` line in a diff is NOT a removed symbol when ruff joined the signature onto one
  line — validate every "removed symbol" smoke assert locally BEFORE shipping it (mine failed
  validation and was wrong).

**Mechanism refinement (reusable):** two-stage rsync from archive staging. Stage 1: full-tree
`rsync -cain --delete` staging→live as the DRIFT AUDIT — fail closed unless the itemized transfer
set == the expected delta exactly (proves no out-of-band writer touched tracked files; 0 deletions).
Stage 2: REAL run with `--files-from=<delta list>` — the full-tree run would also rewrite mtimes on
~800 content-identical files (`.f..t......`) because `git archive` stamps every file with the commit
date, wrecking future mtime forensics; files-from touches only the delta. Then second dry-run empty,
per-file `sha256sum -c`, `__pycache__` purge outside .venv (GNU find: `-prune` and `-delete` are
INCOMPATIBLE — use `-print0 | xargs -0r rm`), import smoke with locally-validated discriminators
(here: `territory.routing_line("MT","webs")==""` new vs unassigned-note old;
`delivery.fallback_to_daily` exists), stamp, PID check.

**What the range changed (semantic set only):** cli.py, slack/drip.py, +slack/drip_card.py,
campaign/{delivery,pacing,card}.py, territory.py. (a) `cli.cmd_drip` now falls back:
`drip[rich]: <outcome>; falling back to the daily card` for eligibility misses AND cutoff misses
(`delivery.fallback_to_daily`); daily card restyled via drip_card.render_blocks. (b) **Hard cutoff
moved 11:00 → 11:30 PT** (pacing.HARD_CUTOFF_PT) — fixes the [[drip-slot-band-vs-cron-granularity]]
trap for the rich path: a slot in 10:31–10:45 can only fire at the 11:00 tick, which the old cutoff
refused. (c) Routing revision: unmapped state renders NO line at all (`routing_line` =
`mention_line`; the "unassigned territory" note is gone — Chase revised 2026-08-05).

**Rollback if needed:** `~/pre-5f09200-overwritten.20260806T070958Z.tar.gz` (41 files, 199 KB) +
`~/.deployed_revision.bak.20260806T070958Z`; also `rm` the 5 added files and purge `__pycache__`.
DB untouched (schema stayed 28; dry-run verification used the read-only URI). Disk was 64% used /
18G free at deploy time — someone freed ~9G since 07-25, not this session's doing.
