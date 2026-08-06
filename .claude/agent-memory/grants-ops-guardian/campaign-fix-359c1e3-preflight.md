---
name: campaign-fix-359c1e3-preflight
description: Campaign-member fix (359c1e3) migrations 27-28 — 28 MUTATES production crm_actions rows (cancels duplicate ready previews); Phase A preflight state verified 2026-07-25
metadata:
  type: project
---

Deploy of **359c1e3fd2b08280623cad767a18f11419e8a69d** (Salesforce Campaign-member fix) over
prod **e8ecf0c**. Phase A read-only preflight verified 2026-07-25 ~21:35Z; every expectation matched.

**MIGRATION 28 IS DATA-MUTATING — do not treat "apply migrations" here as schema-only.**
Read off the rehearsal artifact `~/backups/deploy-359c1e3-20260725T205815Z/rehearsal-migrations-27-28.db`
(opened `mode=ro&immutable=1`):
- 27 = "exact Salesforce Campaign batches and verified writes" — adds 5 tables
  (`crm_campaign_batches`, `_batch_items`, `_batch_targets`, `_write_attempts`,
  `_approval_attempts`) + `crm_actions.batch_id` / `.batch_target_id`. Additive.
- 28 = "one ready Salesforce Campaign creation per thread" — **UPDATEs existing rows**: within a
  thread it keeps ONE ready `create_campaign` and sets the rest `state='cancelled'` with
  `last_error='Superseded by migration 28: duplicate ready Campaign preview'`. In the rehearsal it
  cancelled `1de9fac0…` + `ef622493…` and KEPT `b620bd04…` (all thread_ts `1784819113.594459`),
  taking ready create_campaign 4 → 2. The unrelated playground row `6f90999e…` (channel
  `C0B02721MNK`, thread `1784183540.572109`) is untouched. So a DB backup is mandatory, and the
  rollback story for 28 is restore-from-backup, not a reverse migration.
- Which of the three duplicates survives is decided by the migration, not by me — do not assume it
  keeps the newest/oldest without re-reading the rehearsal.

**Zero-write inspection recipe (reusable).** To compare a backup or rehearsal DB against live without
writing ANYTHING — not even a `-shm` — open it `file:<path>?mode=ro&immutable=1`. `mode=ro` alone still
maps/attaches the `-shm` on a WAL database; `immutable=1` skips WAL/shm entirely. Use plain `mode=ro`
for the LIVE db (it must read the WAL to be current) and `immutable=1` for every static copy.
This proved `grant_watch.db.before` logically identical to live (max_migration 26, integrity ok,
2 FK violations, and 9 table counts all equal) even though its `-wal` sidecar is 0 bytes while the
live `-wal` is 181,312 bytes — a 0-byte WAL beside a copied main file is NOT by itself evidence of a
truncated backup; verify by row counts, don't infer from sidecar size.

**Two PRE-EXISTING FK violations are Chase-approved to leave alone** (`source_observations` rowid
10642 and 11892, both parent `leads`, fkid 0). These are the permanent orphans from the 2026-07-21
duplicate-lead fix — see the CLAUDE.md status entry. `integrity_check` is `ok`. Do not "fix" them.

**Disk note:** the old "prod at 95-97%" warning is STALE. `/dev/vda1` measured 48G total, 27G used,
**22G avail, 56%** on 2026-07-25 (after the 2026-07-22 venv purge in [[disk-footprint-and-cruft]]).
Plenty of headroom for a fresh ~24MB DB backup.

**Rehearsal left the box restored, verified:** a prior session paused cron (staging `crontab.paused`
= a single comment line) and stopped the listener, then restored both. Current crontab sha256
`6275d5025455ddfa1af9dd44715fa13297b7a6a5374b9a9cf5bfc78769a44711` is byte-identical to both
`crontab.before` and `crontab.restored.check`. The pause window is visible as exactly one missing
keepalive tick in cron.log (21:00:01Z → 21:10:01Z, no 21:05Z). Listener PID moved 610788 → 623507.

**GENERAL FACTS worth keeping beyond this deploy (all verified 2026-07-25 by reading source):**
- **There is NO migrate CLI subcommand and NO migration dry-run.** `db.connect()` calls
  `apply_migrations()`; `db.connect_readonly()` deliberately does NOT migrate (it is what `--dry-run`
  uses, so a dry run can never advance schema). The apply entrypoint is therefore just
  `cd ~/grants_agent && .venv/bin/python -c "from grant_watch import db; db.connect()"`. Migration
  modules import ONLY `sqlite3`/stdlib — a migration can never make a network call, and `connect()`
  never reads `.env`, so no Slack/Salesforce client is constructed.
- **TRAP: `deploy_rsync.sh` (in the repo AND on the droplet) pushes FROM the laptop working tree**
  (`/Users/chasengonzales/grants_agent/`), hardcoded, with the droplet IP inline. Never run it to
  deploy a specific revision — it ships whatever uncommitted work the laptop happens to have. Deploy
  from a clean `git archive` export staged on the droplet instead. Its `excludes=()` array is still
  the canonical exclusion set worth quoting.
- **Restoring a SQLite backup: delete `-wal` and `-shm` BEFORE copying the backup over the main file.**
  A stale WAL left beside a restored main file gets replayed and silently re-applies migrated pages.
  Back up with `VACUUM INTO` (refuses to overwrite, folds WAL content in), verify with
  `sqlite3 -readonly`.
- **A tar code snapshot is not a complete code rollback** — tar restores modified files but cannot
  delete files the new revision ADDED. Any rollback must also `rm` the new-file list explicitly
  (359c1e3 adds 15 files) and purge `__pycache__`.

**LIVE-TRAFFIC COUNTERS ARE NOT INVARIANTS (learned the hard way 2026-07-26).** A pre-deploy guard
that pinned `search_requests == 129` tripped because a human used Grant in Slack 14 min earlier
(`search_requests` 129→130, `slack_event_receipts` 325→328, everything else identical). Any table the
live bot appends to — `search_requests`, `slack_event_receipts`, `slack_conversation_threads`,
`engagement` — must be asserted `>=` a floor, never `==`. A person using the product must never look
like corruption. Tables safe to pin `==` while the bot is up: `leads`, `crm_actions`,
`crm_action_items`, `source_observations`, `funding_events`, `posts`, `notification_outbox`.
**Corollary — take the authoritative pre-migration check AFTER quiesce, not before.** With the bot
live there is a real race on migration 28: it keeps MAX(created_at, rowid) per thread, so a NEW ready
`create_campaign` appearing in thread `1784819113.594459` would make 28 cancel `b620bd04` (the row we
intend to KEEP) instead. Gate on "b620bd04 is still the newest in that group" once nothing can write.

**STATUS 2026-07-26T01:45Z — a SECOND guardian run (target 4a4d550) HALTED AT PRE-FLIGHT: a
CONCURRENT WRITER holds the box mid-deploy.** Launched to deploy `4a4d550` (= 359c1e3 + a ruff-format
commit) with the same migrations 27-28, I found at 01:43Z: live crontab **3 lines / 400 bytes /
sha `261f157e…`, watchdog ABSENT**, byte-identical to `~/backups/deploy-359c1e3-20260726T012742Z/
crontab.quiesced` (written 01:41Z, ~2 min before I looked), whose sibling `crontab.before` is the
healthy 470-byte `6275d502…`. A non-guardian tenant session (pid 632267 sshd → 632268 `bash -s` →
632300 `sleep 330`, started 18:41:15 PT) was still resident. Code tree untouched, revision still
e8ecf0c, MAX 26 — the other run is BETWEEN quiesce and rsync. Deploying would have raced it on the
rsync AND on migration 28's per-thread winner selection. **Two writers, one live SQLite DB, same
lineage — the exact collision this file's race note warns about.** Nothing written by me; probe was
100% read-only.
**Reusable preflight gate (add to every deploy):** before ANY mutation, assert (a) crontab sha ==
`6275d502…` AND the `*/5` watchdog line is present, and (b) no foreign `bash -s`/`sleep`/`rsync`
tenant session is resident. A quiesced crontab is not damage — it is another operator's stop→work→
restore window, and the correct response is to WAIT, not to deploy or to "restore" their crontab.
Also: the newest `~/backups/deploy-*` dir is that operator's LIVE rollback material — never treat a
same-day backup dir as "stale rehearsal cruft" to clean up.

**STATUS 2026-07-26T02:12Z — DEPLOYED AND VERIFIED. 359c1e3 IS LIVE, schema 28.**
Two earlier Phase B attempts were DENIED by the Claude Code permission classifier (once at step 1,
once at step 3); both times I halted the whole effort per [[coordinator-stop-is-stop]] rather than
reshaping the command, and production was verified byte-for-byte unchanged after each. Chase then
changed the permission mode and the third attempt ran to completion.
Final state: revision `359c1e3fd2b08280623cad767a18f11419e8a69d`, schema MAX **28**, integrity ok,
FK = exactly the two approved orphans, `ready create_campaign` 4→**2**, the 5 `crm_campaign_*` ledger
tables present and EMPTY, listener PID 633555 single/healthy, crontab restored byte-identical
(sha `6275d50…44711`, 4 lines), `.env` sha256 unchanged (`fe9fd588…f55`). Outage 18:46:50→19:12:19 PT
= **~25.5 min** (I had estimated ~15; the 5.5-min keepalive drain plus per-step verification is the
bulk of it — budget 30 min next time). Rollback artifacts retained in
`~/backups/deploy-359c1e3-20260726T012742Z/`: `grant_watch.db.pre28` (sha `79a918db…76ef`, VACUUM INTO,
verified schema 26) + `code_before.tar.gz` (sha `d974adbc…23ae`) + `crontab.before`.

**PHASE C cleanup done 2026-07-26T02:22Z:** removed the staging dir
`~/.deploy_staging/campaign-359c1e3.20260725T205815Z` (15.5 MB) and the 3 throwaway
`rehearsal-migrations-27-28.db*` files in the OLD evidence dir (23.9 MB) = **39.4 MB** reclaimed
(immaterial: df stayed 56%). RETAINED the live rollback dir `~/backups/deploy-359c1e3-20260726T012742Z`.
`~/.deploy_staging` was NOT removed — it still holds **12 unrelated dirs from 2026-07-15, 274 MB**.
**Known remaining cruft, never yet authorized for deletion (candidate Phase D):** those 12 staging
dirs (274 MB) + **28** `~/.grants_agent.previous.pre-*` snapshot trees (**1.1 GB**) + 17
`grant_watch.db.bak.*`/`.snapshot.*` files (198 MB) ≈ **1.5 GB** of the 2.0 GB home. Nothing is
disk-pressured (22 G free), so this is hygiene, not urgency — and each needs explicit per-path
approval; see the safe-purge recipe in [[disk-footprint-and-cruft]].

**MY VERIFICATION SCRIPT HAD A BUG worth remembering:** comparing a post-migration row against a
pre-migration backup with `select *` raises `KeyError` on any column the migration ADDED (here
migration 27's `crm_actions.batch_id`/`batch_target_id`). Diff only columns present in BOTH schemas,
and assert the added columns are NULL separately. The crash looked like a deploy failure and would
have triggered a needless rollback of a perfectly good migration — always confirm whether a red
result is the SYSTEM failing or the CHECK failing before reaching for rollback.

**INDEPENDENT RE-VERIFICATION 2026-07-26T04:46Z (separate read-only session, all 8 checks passed).**
Confirms the deploy held: crontab 4 lines / sha `6275d502…44711` / watchdog present, ONE listener
(PID 633555, cwd `~/grants_agent`, 53 `.venv` map hits, up since 19:12:19 PT), revision `359c1e3…`,
schema MAX 28, integrity ok, FK = the 2 approved orphans, zero Firecrawl in either log.

**A `ready` crm_actions COUNT IS NOT LIVE EXPOSURE — check `expires_at` before alarming.** All **9**
rows in state `ready` are EXPIRED (oldest 2026-07-15, newest `b620bd04` create_campaign expired
2026-07-24T15:39:57Z, `external_write_started=0` on every one). Approvals are ~15 min; `ready` is
simply the terminal resting state for an un-tapped preview — nothing sweeps it to `expired`, so the
count only grows. The enforcement point is `grant_watch/enrich/salesforce_campaigns.py:524` (verified
present in the DEPLOYED commit via `git show 359c1e3:…`): `require_ready and
datetime.fromisoformat(row["expires_at"]) <= _now()` → `raise TimeoutError`. So with
`SALESFORCE_CAMPAIGN_WRITES_ENABLED=1` armed against PRODUCTION Salesforce, a human tapping any
currently-queued button gets a refusal, not a write. Re-derive this per run — do not cache "9 ready =
safe"; a FRESH preview would be live exposure.

**Quiesce windows are legible in cron.log — measure the gap, don't eyeball the tail.** The deploy is
visible as exactly one 35-min keepalive hole (01:40:01Z → 02:15:01Z), bracketing the code write
(19:03 PT) and listener start (19:12 PT), with 32 clean 5-min ticks after. Gap analysis over the last
~200 ticks is the cheap way to prove a watchdog RESUMED rather than merely "is logging now". An
unrelated 10-min hole (21:00→21:10Z) is the earlier rehearsal pause noted above.

**Harness gotcha when counting the listener:** `pgrep -f 'grant_watch[.]slack[.]grant'` counts your
own SSH shell, because the pattern text sits in that shell's own cmdline — it reported 2, not 1.
Filter to python argv (`ps -o pid=,args= | awk '$2 ~ /python/'`) or the count is off by one.

See [[tenant-and-layout]], [[deploy-mechanism]], [[rich-card-deploy-e8ecf0c]], [[tenant-db-write-safety]].
