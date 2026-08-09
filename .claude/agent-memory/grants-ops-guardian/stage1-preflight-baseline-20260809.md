---
name: stage1-preflight-baseline-20260809
description: Stage-1 preflight baseline for the 90f0420→(3915b11) deploy — exact prod fingerprints, the two rollback artifacts and their sha256s, and the VACUUM-INTO-from-a-read-only-connection backup recipe
metadata:
  type: project
---

Read-only baseline + rollback artifacts captured 2026-08-09T21:05Z, scoped grants SSH only. No
code, `.env`, crontab, process or DB change was made.

**Baseline (all `verified` by direct observation):** `.deployed_revision` =
`90f042062f7fa8c6dff6901674b92a2f95ef390e` (stamp mtime 2026-08-06 14:33 PT); schema_migrations
MAX **28** / 28 rows / 38 tables; `integrity_check` ok; `foreign_key_check` = EXACTLY the two
known-and-approved `source_observations` orphans (rowids 10642, 11892) and no others; listener PID
**1227** since Sun 2026-08-09 03:55:01 PT; disk `/` 48G, 32G used, **17G free, 67%**; `.env` sha256
`5cb3d3b1…9df0`, 57 lines / 2754 bytes / mode 600; crontab sha256 `575fbc7c…1a72`, 10 lines (5
active + 5 comments). Live `grant_watch.db` 26,718,208 B sha `abeef597…c195f` with a hot 9.8 MB WAL
(both untouched by this session — re-hashed identical afterwards).

**Contacts by `contact_status` (the input to migration 29's provenance backfill): 81 total —
`linkedin_only` 36, `not_found` 26, `verified` 19, `unverified` 0.** So after migration 29 expect
`contact_provenance` = `page_verified` **19**, `linkedin_claimed` **36**, NULL **26**, and zero
`vendor_licensed` rows. Any other split means the backfill did not do what its source says.

**Rollback artifacts, `/home/grantwatch/backups/stage1-preflight-20260809T210645Z/` (mode 700):**
- `grant_watch.db.pre-stage1` — 24,920,064 B, sha256
  `63add322 0e2a4097 b85a9fb4 e8ea1203 b0a76b2c 9ba44104 e751c20b e3cbfa6f`. Verified by opening the
  COPY: integrity ok, schema 28, 38 tables, the same two FK orphans, contacts 81 (19/36/26), leads
  10,715, rich_card_snapshots 3, crm_actions 48.
- `code_at_90f0420.tar.gz` — 4,045,324 B, sha256
  `62502a70 a0bcdee6 3320a2db 46b0e5a1 3ee389c7 c018d87e 8f9e19e8 46362140`, 1011 members, 90
  `grant_watch/**/*.py`, `gzip -t` OK. Audited to contain ZERO `.env`, `secrets/`, `*.db*`, `.venv`,
  `.git`, `__pycache__` members; DOES contain `.deployed_revision` and `run_bot.sh`.
  `secrets/` (the Salesforce JWT key) is deliberately excluded — deploys never touch it, so
  excluding it costs nothing and avoids duplicating a private key on disk.

**How to apply:** a code rollback restores the tar AND must re-`rm` files the new commit adds (tar
cannot delete), purge `__pycache__`, and — because migration 29 MUTATES data — restore the DB copy
with the live `-wal`/`-shm` deleted first, or `db.connect()` silently re-applies it.

**Backup recipe worth reusing:** `VACUUM INTO` runs fine on a `mode=ro` URI connection against the
live hot-WAL DB — it is read-only with respect to the source (proven: source mtime_ns + size
identical before/after, and the live sha256 unchanged). It also compacts (26.7 MB → 24.9 MB). No
writable connection is ever needed for a backup, so [[readonly-db-forensics-recipe]]'s zero-write
guarantee extends to backups. Verify the COPY (integrity + schema + row counts), never the original.

See [[deployed-vs-local-drift-20260809]] for what is undeployed; [[env-zoominfo-20260809]] for the
two env-var deploy prerequisites, both confirmed ABSENT from the droplet in this preflight.

**SUPERSEDED IN PART — Stage 2 executed 2026-08-09T21:12Z.** The `.env` fingerprint above
(`5cb3d3b1…9df0` / 57 lines) is now the PRE-image only; live is `f4abd546…2a99` / 66 lines / 32
keys, because `GRANT_SALESFORCE_WRITE_CHANNEL_IDS` and `ZOOMINFO_MONTHLY_CREDITS` were appended.
Everything else here — revision 90f0420, schema 28, PID 1227, crontab `575fbc7c…1a72`, both
rollback artifacts — is UNCHANGED and still the Stage 3 baseline.
