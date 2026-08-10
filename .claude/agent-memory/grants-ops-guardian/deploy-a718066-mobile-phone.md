---
name: deploy-a718066-mobile-phone
description: Deploy d664548→a718066 on 2026-08-09 (CURRENT PROD) — schema 36→37 (contacts.mobile_phone), PID 26876, 0.91s outage; A/B wordings proven distinct on the deployed bytes; DB rollback is no longer free
metadata:
  type: project
---

**LIVE 2026-08-10T01:42:50Z (droplet Sun 18:42 PT).** Production moved
`d66454838036936d3336a6aea6e8a18084c4e3b8` → `a7180669f670e756a7424980a51cdec851a0a262`
(4 commits). **Schema 36 → 37.** Listener **25636 → 26876** (OLDPID == the PID
[[deploy-d664548-followups-live]] recorded ⇒ no out-of-band restart). Outage **0.91 s**
(T0 01:42:49.514 → new PID 01:42:50.422). Fresh log region exactly 2 lines
("Grant is listening" + "Bolt app is running"), 0 tracebacks, PID_COUNT 1, uid 1001,
cwd `/home/grantwatch/grants_agent`, 53 venv maps.

## Shape

Delta 22 paths = **17 deployable** + 5 `.claude/agent-memory/**` (never deployed).
16 modifications + **1 add** (`tests/test_lead_data.py`, proven absent beforehand).
Archive `7bc961d1…0db1`, 368,640 B; member set (after filtering `git archive`'s
directory entries) asserted EXACTLY equal to the intended 17 and every member hashed
against `git show a718066:<path>` before leaving the laptop. **Pre-image: all 16
overwritten files hashed byte-exact at d664548 ⇒ no out-of-band drift.** Staged →
`rsync -cai --no-times --no-perms`, itemize 16 `>fcsT`/`>fc.T` + 1 `>f+++++++++`,
0 deleting, catch-all empty, `find -cnewer` exactly 17, second dry run empty.

**`--delete` is WRONG for a staging-dir rsync, and the "preview deletions first" habit
from full-tree deploys will mislead you.** The staging dir holds only the delta, so
`-cain --delete` would propose removing the ENTIRE rest of the live checkout. Run the
preview WITHOUT `--delete` and assert `grep -c '^\*deleting'` is 0 instead.

## The zsh `:gr` trap is not just an rsync-destination problem

`git show $r:grant_watch/migrations.py` inside a zsh loop failed as
`unknown revision 'd664548ant_watch/migrations.py'` — the same csh history modifier
`:gr` that eats an rsync destination's host also eats a **git revspec's** path after
the colon. **Brace every `"${rev}:path"`**, exactly as for `"${h}:grants_agent/"`.
Related: [[deploy-mechanism]].

## Counting migrations: grep the registry literal and you will get the wrong number

`awk '/^MIGRATIONS: tuple/,/^\)$/' | grep -c '^    Migration($'` gave 25 → 26, but the
authoritative runtime count is **33 → 34** entries with `MAX_REGISTERED_VERSION 37`.
Ask the interpreter (`len(M.MIGRATIONS)`, `max(x.version …)`, `any(x.version == 37 …)`),
not the source text — the registry is not wholly one grep-able literal.

## Migration 37 — additive, and REQUIRED by the code that shipped with it

One guarded `ALTER TABLE contacts ADD COLUMN mobile_phone TEXT` (nullable, no default,
`notnull=0`). Applied with the bot **DOWN** (a restart never applies migrations).
Verified after: MAX 37, `schema_migrations` row 37 stamped 01:42:49Z, column present as
TEXT, **85 contacts and NONNULL_mobile_phone = 0 ⇒ no existing row was rewritten**,
tables still 46, leads 10715, `integrity_check` ok, `foreign_key_check` still EXACTLY
the two approved orphans (10642, 11892).

**Not optional.** `db_contacts.save_vendor_contact`'s INSERT now names `mobile_phone`,
so on a schema-36 DB any ZoomInfo paid enrichment raises. Code and migration land together.

## What the deploy actually fixes

A ZoomInfo `mobilePhone` was being collapsed into `contacts.phone` via
`mobile_phone or direct_phone`, and `salesforce_contact_records` copies `phone` straight
into a Lead's **Phone** field — so a rep read a mobile as a desk line. Same family as the
already-fixed "switchboard number next to a person's name". Also: the campaign
confirmation now hands back the Lightning link (`_campaign_link`, best-effort, a missing
link never fails a completed write).

## The A/B wordings are now genuinely distinct — proven in prod

Ran on the DEPLOYED bytes (`nudges.py` sha `5f6ccda2…f399`, == the a718066 blob):
`card_unengaged` / `target_slack=""` / `observed={"entity_name":"Test District"}` →
A "Anyone want Test District? Nothing's come back here…" vs B "Test District is still
unclaimed. Shall I track down a contact for it, or let it go?" — `a != b` PASSED.
Also differ=True for `crm_batch_blocked`, `crm_batch_partial`, `crm_preview_expired`,
`thread_abandoned`, and the mention-carrying `card_unengaged`. This retires the defect in
[[nudge-variant-ab-is-inert]] for these six paths. **`capability_now_available` and
`card_escalated` still ignore the variant argument entirely** — they delegate to
`_capability_message` / `_escalation_message`, which take no variant. If either ever gets
tagged, the ledger would compare a sentence with itself again.

## Read-only checks, verbatim

- `nudge --dry-run --force` → `nudge: [dry-run] would nudge card_unengaged (a): Anyone
  want Wilder School District #133? …` (still an UNMENTIONED threaded reply at the head
  of the queue — re-read eligible #0's `target_slack` before any forced run).
- `remind --dry-run` → `remind: skip: nothing due`
- `nudge-report` → `No follow-ups have been delivered yet, so there is nothing to compare.`
- Rows: `followup_nudges` **0**, `capability_asks` **5**, `reminders` **1**.
  `capability_asks` by capability: campaign_load 2, contact_supplied 1, email_results 1,
  reminders 1 — **`SUM(available_since IS NOT NULL)` is 0 for every one**, so no ask can
  produce a message yet.
- Inertness re-proven: DB mtime AND size identical before/after all five checks
  (1786326169 / 26931200).

## Postflight fingerprints

Crontab **byte-identical** to the pre-deploy backup, sha
`63495d445812caaec8b8c1b086e72aa8db2bcd249730a5400e07bc61042ea1f7`, 12 lines = **7
active + 5 comments**; the `15 9,14 * * 1-5 … nudge --execute` line added last deploy is
still present. `.env` byte-identical, sha `9b68bc18…c634`, 67 lines / 33 keys.
`run_bot.sh` `07773019…06bb` unchanged. Full import closure: bot closure 21 modules,
**112 grant_watch modules on disk, 0 import failures**. Disk 67%, 16 G free;
`~/backups` 253 M.

## Rollback artifacts (700/600) — and the DB restore is NOT free

`~/backups/deploy-a718066-20260810T014027Z/`:
- `grant_watch.db.vacuum` 25,088,000 B sha
  `33e7dc3420c82017e24991ef751718347f8f71d1888296f8388e73e82eac3d8c`
  (COPY verified: integrity ok, schema 36, 46 tables, leads 10715, contacts 85, same 2 FK orphans)
- `code_at_d664548.tar.gz` 99,141 B sha
  `6b743c8f441c562752f937eb41f29363a2d45819942e191bfee2f265f712f4af`, 16 members, `gzip -t` OK
- `env.bak` (== `9b68bc18…c634`), `crontab.bak` (== `63495d44…a7f7`), `deployed_revision.bak` (40 B)

Rollback = restore the tar, `rm tests/test_lead_data.py`, re-stamp d664548, restart.
**Do NOT restore the DB copy casually.** Unlike the code, the DB carries work the
PREVIOUS deploy wrote — `org_*` enrichment on 21+ gold leads and the 5 seeded
`capability_asks` — and restoring would discard it. Migration 37 is additive, so a code
rollback does not require a DB rollback: an unused nullable column is harmless to
schema-36 code. Note the `.vacuum` sha DIFFERS from d664548's (`56012a42…`), which is
expected — that deploy's own writes plus live traffic moved the DB.

Related: [[ssh-rate-limit-and-stdin-traps]] (bit again — see below),
[[deploy-mechanism]], [[nudge-variant-ab-is-inert]].
