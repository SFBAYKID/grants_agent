---
name: migration-version-collision
description: Droplet DB carries SIDE-LINEAGE migration numbering; main's migration 9 (org_* cols) is masked and never applies — org_* columns missing
metadata:
  type: project
---

Discovered 2026-07-17 while deploying main `e6df182` (delta `ed261ff..e6df182`). The droplet
`grant_watch.db` `schema_migrations` ledger uses the **side-lineage** numbering (from
`origin/codex/grant-new-chat-salesforce-fixes-v2`, deployed then rolled back to main), which
diverges from main starting at version 8:

| ver | main lineage (e6df182)              | droplet ledger (side lineage)        |
|-----|-------------------------------------|--------------------------------------|
| 8   | Salesforce follow-up reminder state | complete search snapshots            |
| **9** | **organization profile columns (org_\*)** | **Salesforce follow-up reminder state** |
| 10  | (none)                              | tenant-scoped user preferences       |
| 11  | (none)                              | thread-bound LinkedIn person candidates |
| 12  | (none)                              | versioned reference enrichment coverage |

**Consequence:** the runner keys on version NUMBER only. Ledger has 1-12 all present, so
`e6df182`'s `_migration_9_organization_profile` is **SKIPPED forever** — the 10 `org_*` columns
were **never added to `leads`** (verified: `PRAGMA table_info(leads)` shows none). Meanwhile
`salesforce_followup_state` + the 3 side-lineage tables (`user_preferences`,
`linkedin_person_candidates`, `reference_enrichment_runs`) all PRESENT and harmless under main code.

**Blast radius (verified in e6df182 source):** core lead reads are SAFE (`get_lead`/`_LEAD_EVENT_SELECT`
use `l.*`; upsert/listings use `SELECT *` — none NAME org_*), and the bot starts clean. BROKEN only
when the new org-profile feature runs: `db.save_org_profile` (UPDATE names org_*) and
`organization_profile.py` (`lead["org_website"]` subscripts) → `no such column` / IndexError. That
feature is `needs-testing` (not in the guaranteed weekly cron), so existing flows keep working.

**Remediation — APPLIED 2026-07-17 (all verified).** This specific one-time column-add was authorized by
Chase for that run only; it does NOT create standing permission — any future prod-DB write still needs the
operator's explicit per-run approval. Steps: backed up db+wal+shm as a set to
`~/grant_watch.db.bak.20260718T024830Z*` (live-.db sha256 matched pre-edit), quiesced the bot
(`pkill -f 'grant_watch[.]slack[.]grant'`, cron keepalive relaunched it after), then applied migration 9's
exact defs by hand via `.venv/bin/python` sqlite3 under one `BEGIN IMMEDIATE` txn: `ALTER TABLE leads ADD
COLUMN` for org_website, org_general_email, org_phone, org_street, org_city, org_state, org_postal_code,
org_profile_status, org_profile_source_url (TEXT) and org_student_count (INTEGER) — idempotent skip-if-present.
Result: leads org_* count 0→10 (verified `PRAGMA table_info` + module smoke `ORG_COL_COUNT_INT 10`), read-back
`SELECT org_website, org_profile_status` clean, `grant_watch.enrich.organization_profile` imports OK.
`schema_migrations` LEFT UNTOUCHED — v9 row is still `(9,'Salesforce follow-up reminder state',
'2026-07-15T20:41:03+00:00')`, 12 rows total (runner keys on version number, skips 9 forever, now HARMLESS
because the cols exist). **Future landmine STILL OPEN:** when main later adds migrations 10/11/12 they will
ALSO be masked by the side-lineage's 10/11/12 already in the ledger — owner must decide a real reconciliation
before shipping any main migration ≥10. See [[tenant-and-layout]] and [[tenant-db-write-safety]].

**LANDMINE FIRED 2026-07-19 (deploy HALTED, no mutation).** A deploy of main `ed0ffc6` ("fix: allow
platinum/rfp drip posts (posts.kind CHECK) + harden post-send") ships main's migration **10**
(`_migration_10_widen_post_kinds`: rebuild `posts` to widen CHECK to
`kind IN ('platinum','nugget','rfp','bulletin')`). The task assumed droplet `MAX(version)=9`. Live
read-only inspection proved otherwise: ledger still has 12 side-lineage rows, MAX=12, and **version 10 is
the side-lineage "tenant-scoped user preferences"** — NOT the posts widen. `apply_migrations` keys only on
version NUMBER (`pending = [m for m in MIGRATIONS if m.version not in applied]`), so with `applied={1..12}`
every main migration 1-10 is already "applied" → `pending==[]` → migration 10 is **silently skipped, never
runs**. Verified live: `.deployed_revision`==ba0a7b7 (matches task), posts SQL still
`CHECK(kind IN ('nugget','bulletin'))` (NOT widened), 17 posts (4 bulletin/13 nugget). Deploying+restarting
as written would be a FALSE SUCCESS: bot starts clean, but the CHECK stays narrow and drip keeps crashing on
platinum/rfp. STOPPED and returned to Chase for a reconciliation decision. Scoped options proposed (NONE
executed — a prod-DB reconciliation needs Chase's explicit per-run approval + backup-first): (A) renumber
main's migration to the next free droplet version so it isn't masked (code change, re-skews fresh DBs);
(B) hand-apply the posts rebuild on the droplet under backup + `BEGIN IMMEDIATE`, ledger untouched — same
precedent as the 2026-07-17 migration-9 remediation above; (C) a real ledger reconciliation. See
[[deploy-mechanism]].

**RESOLVED 2026-07-20 via Option A (all verified).** Chase renumbered the widen-post-kinds migration 10→**13**
(commit `ac2f030`, then folded into `aa09dca`), the next free number above the droplet's side-lineage max of
12. On the droplet `applied={1..12}` so version 13 was genuinely pending and ran. Applied via a one-shot
migrating connect (`.venv/bin/python -c "from grant_watch import db; db.connect().close()"`) after a fresh
backup — NOT via bot restart (restart doesn't migrate, see [[deploy-mechanism]]). Verified: schema_migrations
MAX 12→13, posts COUNT 17 preserved, `foreign_key_check` clean, posts CHECK widened to
`('platinum','nugget','rfp','bulletin')`, running bot undisturbed (same PID, no traceback). STILL-OPEN
tradeoff: on a FRESH/main-lineage DB this migration is version 13 (not 10), so the droplet ledger and a
fresh DB now diverge on which number = "widen post kinds"; main migrations 11/12 remain unused numbers on
the droplet. Any FUTURE main migration must still pick a number not in the droplet's ledger (now includes 13).
