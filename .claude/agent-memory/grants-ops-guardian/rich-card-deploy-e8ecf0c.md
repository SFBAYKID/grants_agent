---
name: rich-card-deploy-e8ecf0c
description: Rich-award-card deploy (e8ecf0c) COMPLETED 2026-07-24 flag-OFF; needed NEW venv dep tldextract 5.3.1; migrations {14-26} applied (MAX 26) collision-free on the side-lineage ledger
metadata:
  type: project
---

The committed rich-award-card feature (revision **e8ecf0c**, "review/rich-award-card-campaign"
lineage) deploys `grant_watch/campaign/*`, `grant_watch/slack/proactive_actions.py` (unconditional
`register(app)` in `create_app`, so button handlers load regardless of the flag), `migrations_rich.py`,
`roster.py`, `enrich/salesforce_activity.py`, and migrations **14-26**. Flag `GRANT_RICH_CARD_ENABLED`
gates DELIVERY only (`campaign/__init__.py`, defaults OFF); handler registration is NOT gated.

**BLOCKER — a NEW runtime dependency not in the droplet venv:** `requirements.txt` at e8ecf0c adds
`tldextract>=5.1` (Public Suffix List for eTLD+1 org binding). Only `campaign/policy.py:24` imports it,
and every `campaign` import in the app is LAZY (inside functions) + the delivery chain is gated behind
`if rich_card_enabled():`. Consequences (all verified 2026-07-24 by import trace + live `.venv/bin/python`):
- Bot BOOT is unaffected — `import grant_watch.slack.grant` + `create_app`/`register(app)` do NOT import
  policy; `import grant_watch.campaign` (the `__init__`, only imports `os`) succeeds. Only
  `grant_watch.campaign.actions`→`.snapshot`→`.policy`→`import tldextract` raises `ModuleNotFoundError`.
- Cron `drip`/`poll` (flag OFF) do NOT import tldextract: `cli.cmd_drip` imports only `rich_card_enabled`
  from `__init__`, and takes the `else`→`slack.drip.run_drip` branch; `from .campaign import delivery`
  is INSIDE the `if rich_card_enabled():` block. So a missing tldextract does NOT crash bot or cron.
- What it DOES break: rich-card button CLICKS (`rich_persequor_draft`/`rich_not_relevant` in
  proactive_actions.py lazily `from ..campaign import actions`) raise ModuleNotFoundError → buttons still
  error. **So the deploy's goal (functional buttons) requires installing tldextract first:**
  `.venv/bin/python -m pip install 'tldextract>=5.1'` (the `.venv/bin/pip` wrapper is broken — use
  `python -m pip`; see [[deploy-mechanism]]). NOTE tldextract fetches/caches the PSL over the NETWORK on
  first use — a new tenant outbound fetch; a locked-down redeploy may want `TLDEXTRACT_CACHE`/offline
  `suffix_list_urls=()` consideration.

**Migration clearance {14-26} — VERIFIED collision-free live (read-only) 2026-07-24, safe to apply:**
droplet `schema_migrations` = {1-13} (side-lineage 8-12 + main's renumbered 13; see
[[migration-version-collision]]). e8ecf0c `MIGRATIONS` = {1-9, 13, 14…26} (10-12 intentionally skipped).
So `pending = {14-26}` — all 13 run, ending at MAX **26** (matches the task's expected MAX). Pre-checked:
all 8 new rich table names ABSENT (`rich_card_snapshots/_actions/_snapshot_truth`, `contact_evidence`,
`salesforce_activity_snapshots`, `organization_kind_evidence`, `paid_enrichment_attempts`,
`proactive_daily_slots`); `posts` has EXACTLY the 11 cols migration 15's rebuild SELECTs
(id,kind,lead_id,channel,ts,style,posted_at,event_id,delivery_key,delivery_status,urgent) and CHECK still
4-kind (15 widens to add `rich_award`); `notification_outbox.snapshot_id`/`runs.state`/`leads.last_confirmed_*`/
`leads.nces_website`/`salesforce_matches.owner_id/owner_email` all ABSENT and ready to add; leads org_*=10
intact; integrity ok. `apply_migrations` is per-migration transactional (`BEGIN IMMEDIATE`→apply→record→
commit; rollback+raise on error, FK off for the run) so a failure halts cleanly. `_add_column` is
idempotent (skip-if-present). Because `cmd_drip`'s non-dry-run `db.connect()` migrates, a normal cron drip
tick will SELF-APPLY 14-26 once e8ecf0c code is on disk.

**2026-07-24 deploy — COMPLETED in two passes (playground test, flag OFF), all verified:**
Pass 1 (staging): rsynced e8ecf0c from a clean `git archive` export (`deploy_src`, NOT the working tree —
which carried unrelated uncommitted campaign-batch + migration_27 work that had to stay off) — 55 files
(29 add/26 mod), 0 deletions, all 55 sha256==e8ecf0c blobs, `.env`/`.db`/`run_bot.sh`/`secrets` untouched.
Import-smoke exposed the tldextract blocker; per "stop on surprise," I tried to roll back and the rollback
script was BLOCKED by the permission classifier ([[coordinator-stop-is-stop]]) — so I halted and reported
rather than improvising another shape. Chase then authorized Option A (complete forward).
Pass 2 (completion): `.venv/bin/python -m pip install 'tldextract>=5.1'` → **tldextract 5.3.1** in the tenant
venv (deps filelock 3.32.0 + requests-file 3.0.1 added; requests/idna already present). Verified
`from grant_watch.campaign import actions` (the button handlers' exact lazy chain) + full boot chain import
clean. The ~15:00 PT cron `drip` tick had already SELF-MIGRATED the DB to MAX **26** via its non-dry-run
`db.connect()` (posted nothing — "skip: daily cap reached (1)"); a bot restart does NOT migrate (see
[[deploy-mechanism]]), so the cron self-heal is what applied 14-26. Verified post-migration schema: MAX 26,
all 8 rich tables present, `posts` CHECK admits `rich_award`, `leads.nces_website` present, integrity ok.
Stamped `.deployed_revision`=e8ecf0c…46cc9; restarted (OLDPID 2890642→**NEWPID 597044**, single, venv
cmdline, 43 `.venv` maps hits, "Bolt app is running!"/"listening (Socket Mode)", NO traceback →
`register(app)` loaded). Final: flag `GRANT_RICH_CARD_ENABLED` still UNSET/OFF (`rich_card_enabled()`
False), cron unchanged (4 lines), `cli drip --dry-run` = old `run_drip` path ("skip: daily cap reached (1)",
exit 0), `posts.kind='rich_award'` count 0 (no rich card posted), production channel untouched.
Rollback if ever needed: consistent snapshot `~/grant_watch.db.snapshot.20260724T214420Z` (MAX 13) +
`~/pre-e8ecf0c-overwritten.20260724T214420Z.tar.gz` (26 mods) + adds list; `~/.deployed_revision.bak.<stamp>`
=99c0240. See [[deploy-mechanism]], [[tenant-and-layout]].
