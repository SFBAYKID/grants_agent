---
name: deploy-d66802b-card-comma
description: 4c6a543→d66802b deployed 2026-08-06 ~19:49Z, 2-file surgical rsync, NO restart (campaign/card.py provably outside the bot's import closure); the --post that followed was classifier-blocked and stopped
metadata:
  type: project
---

**Production `4c6a543` → `d66802ba19be5a888b9cd4b5e7ceddfb3a13e105`** (rich-card fallback_text no
longer emits a dangling comma for an untitled contact). All verified, NO bot restart, NO outage,
PID 633555 held throughout.

Artifact: full-tree `git archive` of d66802b, sha256
`a7f102c2c88eaf118dac71564a0f8429e493ef13ccc1cd8885f5ce0704645906`, 912 files, 0 symlinks, only
`.env.example` matches `^\.env`. Delta = exactly 2 mods (`grant_watch/campaign/card.py`,
`tests/test_rich_card.py`). Post-deploy shas `a28a904a…` / `f380c53e…`. `.env` sha `b3f338ff…c3bff`
AND mtime `1785995257` unchanged, crontab `70e309aa…876f` 5 lines unchanged, run_bot.sh
`07773019…` unchanged, schema stayed 28. Backups: `~/pre-d66802b-overwritten.20260806T194945Z.tar.gz`
(2 members) + `~/.deployed_revision.bak.20260806T194945Z`.

**NO RESTART NEEDED — settled on evidence, not assumption (reusable check).** The question was
whether the long-lived bot holds a stale `campaign/card.py`. Three proofs: (1) importing EVERY
module under `grant_watch.slack.*` (all succeeded) leaves `grant_watch.campaign.card` absent from
`sys.modules`; (2) the full lazy button closure (`slack.proactive_actions` → `campaign.actions`)
reaches campaign/{actions,policy,routing,snapshot} but NOT card; (3) `campaign/actions.py` imports
`db, persequor_client, roster` + `snapshot.{FrozenSnapshot,load}` only, and `mark_not_relevant`
just does `INSERT OR IGNORE INTO rich_card_actions` — the button path never renders. `card.py`'s
only consumers are fresh processes (`cli drip` per cron tick, one-off scripts), so a file swap is
picked up on the next invocation.

**Verification trap that nearly produced a false PASS:** the artifact proof ran `git hash-object`
with cwd inside the extracted artifact dir, which is NOT a git repo — `git ls-tree` emitted nothing,
the loop body never ran, and the script printed "PASS" over **0 files compared**. Always assert
`count > 0` before declaring a comparison passed, and run git with `-C <repo>` when hashing files
that live outside it. Re-run correctly: 912 entries compared, 0 mismatches.

**Two-stage rsync held again** ([[deploy-5f09200-fallback-routing]]): stage-1 full-tree
`-cain --delete` drift audit = 0 deletions, exactly 2 `>fcst......` content changes, 860
`.f..t......` mtime-only lines (that mtime noise is `git archive` stamping the commit date, and is
itself the proof no out-of-band writer altered tracked CONTENT); stage-2 real run with
`--files-from`. GNU find `__pycache__` purge via `-print0 | xargs -0r rm -rf` (`-prune`+`-delete`
are incompatible).

**Then the effort STOPPED.** Chase authorized posting the card; the `--post` invocation was denied
by the Claude Code permission classifier, and so was the read-only DB check that followed. Per
[[coordinator-stop-is-stop]] the command was NOT reshaped. Preview (no `--post`) had already proven
the fix live in production: `Contact: Dalton Cagle — <redacted>@hoxieschools.com.` with no comma.
Left on the box for a re-issued instruction: `~/grants_agent/_oneoff_repost_rich_card.py`,
`~/d66802b.tar`, `~/.deploy_staging/d66802b-20260806T194849Z/`, and helper files
`~/.deploy_delta.txt`, `~/.deploy_stage_path`, `~/.deploy_stage1.txt` — all need deleting once the
work resumes or is abandoned.
