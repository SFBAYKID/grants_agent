---
name: deploy-2159d67-resend-test-email
description: Deploy f894801→2159d67 on 2026-08-10 (CURRENT PROD) — code-only, schema stayed 38, PID 33390, 0.22s outage; both f894801 findings closed (rep mail now reaches the rep, corrections re-seeded 236→176); and the trap that `capability-seed` prints "0 recorded, 5 already on file" on the run that DID update them
metadata:
  type: project
---

**LIVE 2026-08-10T04:53:40Z (droplet Sun 21:53 PT).** `f894801e9d078547b74655059b40d7af152d99a4`
→ `2159d6758d087b67ea73297688661d486057b7b7` (1 commit). **Schema stayed 38** — no migration in
the delta (`git diff -- grant_watch/migrations*.py` empty, and `SCHEMA_MAX` read 38 before, after
the rsync, and after the `--execute` re-seed, which uses the MIGRATING `db.connect()`).
Listener **32750 → 33390**, **0.218 s outage**. Crontab and `.env` byte-identical throughout.

## Fingerprints now

Revision `2159d67…7b7` (41 B *with* trailing newline). Schema **38**, `schema_migrations` 38,
**47 tables**, `integrity_check` ok, `foreign_key_check` exactly the two approved orphans
(10642, 11892). leads 10715. `.env` `9b68bc18…c634` / 67 lines / 33 keys / 600 — **unchanged**.
`run_bot.sh` `07773019…06bb`. crontab `cd38cc6e…6cc5` (13 lines, 1375 B). Disk 68%, 16 G.

Delta 9 paths = **5 deployable** + 3 `.claude/agent-memory/**` + 1 `.env.example`. All 5 are
modifications, zero adds/deletes. Pre-image: all 5 hashed to the `f894801` blobs ⇒ clean base.
Archive `e7fd1d24…e7e6`, 92,160 B, file-member set asserted EQUAL to the delta, upload byte-exact.
`rsync -cai --no-times --no-perms --files-from` → 5 `>fcsT`, **0 deleting**, catch-all empty,
second pass 0 lines, `find -cnewer` exactly 5, post-image 5/5 == `2159d67` blobs.
Import closure **115/115, 0 failures**. Post-restart log region = exactly the 2 boot lines,
**0 tracebacks**, PID_COUNT 1, 53 venv maps, `status=restart_attempt` in `cron.log` at 04:53:40Z.

Whole-code-closure drift check (`grant_watch/**/*.py` + `config/*.json` + `data/announcements` +
`data/capability_asks` = **119 files**): **119/119 byte-identical to `2159d67`.** Worth the one
round trip — it is what turns "the deployed bytes" from an assumption into a measurement.

Rollback artifacts (700/600) — `~/backups/deploy-2159d67-20260810T045108Z/`:
- `grant_watch.db.vacuum` 25,108,480 B sha `2b7ddb68eafe977e963b02c58743920816631a1efa095002c3a1cb76fc936ff4`
  (schema 38 copy, integrity ok, 5 capability_asks). First deploy in six whose DB sha DIFFERS from
  the previous one — live traffic wrote in between, which is the expected reading.
- `code_at_f894801.tar.gz` 18,985 B sha `c42f5b39d0d381db2d9c6ec70f7c75eaf0b5443827ca6db79b21a043537876fd`, 5 members, `gzip -t` OK
- `deployed_revision.bak`. **No `env.bak`** — `.env` was not touched, and fewer credential copies
  on the box is strictly better ([[deploy-mechanism]]).

## TRAP — `capability-seed` prints "0 recorded" on the run that DID update

```
capability-seed: 0 recorded, 5 already on file
```
That is the output of the **successful** re-seed. `cmd_capability_seed` counts only INSERTs;
`record()`'s new duplicate branch UPDATEs `correction` and still returns `None`, so the update is
invisible in the summary line. An operator reading only that line concludes nothing changed —
**the same shape as the bug this deploy fixes.** Verify with the DB, never the CLI summary:
`SELECT id, capability, LENGTH(correction) FROM capability_asks`.

Measured before → after: `[118, 177, 0, 0, 0]` → **`[58, 57, 0, 0, 0]`**. `ask_text` lengths
`[41, 96, 54, 39, 164]` **identical before and after**, and `available_since` / `state` /
`created_at` / `recorded_by` / `thread_ts` / `message_ts` all unmoved on all 5 rows. The UPDATE's
WHERE clause is `(audience, message_ts, capability)` — exactly the table's UNIQUE key from
migration 34, so it can only ever hit the one row that collided. Rows 3/4/5 have an empty
`correction` in the seed file and the `if correction.strip()` guard leaves them alone.

## The email fix, proven on the deployed bytes

`recipient_for()` called for all six roster ids under `load_dotenv`: Chase, Brett, Kerry, Anthony,
Nelly and Jocelyn each resolve to **their own** mailbox. `OUTREACH_TEST_EMAIL` is still SET and
still read by `persequor_client` and `campaign/actions` (prospect guard intact); `RESEND_TEST_EMAIL`
is unset, and setting it in-process redirected correctly then released — so the switch was **moved,
not deleted**. A non-roster id is still refused. Values never printed.

**`/proc/<pid>/environ` is the check that matters here.** `run_bot.sh` sources `.env` in *bash*,
while my probe used python-dotenv — the two parsers disagree about `RESEND_FROM_EMAIL=Grant <…>`,
so a green `load_dotenv` reading proves nothing about the live bot. All 33 `.env` keys are present
in PID 33390 and `RESEND_API_KEY`/`RESEND_FROM_EMAIL` are both non-empty. The
`.env: line 68: syntax error` pair in `cron.log` is **historical** (2026-08-10T00:02:39Z, the
b4a8046 restart, repaired in 14221fc); my 04:53:40Z restart emitted none.

## Two wrong-name probes that read as failures

Same family as [[row-get-wrong-column-false-null]], and both would have been reported as product
defects if I had trusted the first output:
- `write_channel_allowed` lives in **`grant_watch/enrich/salesforce_campaign_policy`**, not `db` —
  `db.write_channel_allowed` raises `AttributeError`. Re-run correctly: exactly
  `C01DGT9D11D` + `C0B02721MNK` True; truncated, lower-cased, unlisted and `""` all False.
- `SalesforceOrganizationIdentity`'s field is **`organization_id`**, not `record_id`. My
  attribute error printed "verify_write_scope FAILED" for a call that had already **PASSED**.
  Real reading: `is_sandbox=False`, org last4 `8EAM`, instance `USA598`, name `Monarch`,
  `EXPECT_SANDBOX=0`, `CAMPAIGN_WRITES_ENABLED=1` — production writes armed.

**A failure message from a probe you just wrote is a claim about your probe first.**

## Also measured (2026-08-10, read-only)

- **All six roster reps resolve to exactly one ACTIVE production Salesforce User** via
  `requester_owner` (Chase `00541000001dACEAA2`, Brett `005UZ00000BnyUXYAZ`, Kerry
  `0052M000007hcy7QAA`, Anthony `0052M000009bDfKQAU`, Nelly `0055d00000Ce6LAAAZ`, Jocelyn
  `005UZ0000034LlhYAE`). The roster trap that stopped Nelly and Jocelyn is closed for everyone.
- **Monday's queue: 44 candidates, 25 `stale`, 19 eligible.** The 5 capability asks are eligible
  positions **0-4** (queue 24, 25, 26, 27, 29). Monday delivers **Kerry then Jocelyn** — *not*
  Kerry twice; her `reminders` ask sorts 5th behind the three 23-July asks. Rendered lengths
  176 / 154 / 139 / 165 / 199, every one under 220 and every one ending in a question. Variant
  **b** now differs from **a** for `capability_now_available` (126 vs 176) and also asks one.
- **Slots for Mon 2026-08-10: `C01DGT9D11D` → 09:54 + 14:11 PT** (playground 08:32 + 13:51),
  both reachable on the `*/30 8-15` cron (ticks 10:00 and 14:30) and both inside Kerry's Eastern
  08–18 gate. `MAX_NUDGES_PER_DAY=2`, `MIN_GAP=4h`, `DROP_AFTER=14d` (drop_after 2026-08-24).
- `followup_nudges` still **0 rows**; `announcements` still exactly **1 row, `posted_at IS NULL`**.
- `followup_optouts` holds one row — Chase's own playground test, **`active=0`** — so nobody is
  currently opted out, and the set→clear round trip is evidenced in the data.
- **`SLACK_WORKSPACE_ID` is still unset**, and it gates ONLY `campaign/actions._authorized_snapshot`
  (the rich-card snapshot buttons, which today render with no actions block at all). It is the
  whole-tree extent of that variable — **two** references — so it does **not** gate the Salesforce
  campaign Confirm path. [[slack-workspace-id-missing]] should be read with that scope.

## `.env.example` has NEVER been deployed, and the hashes prove it

The droplet's `.env.example` is `8fb7b1f5…` — matching **neither** the `f894801` blob (`57b223a8…`)
**nor** the `2159d67` blob (`d3761542…`). The standing exclude list's `.env.*` pattern skips it on
every deploy, so it has drifted for weeks. I kept the convention and did not ship it; the new
`RESEND_TEST_EMAIL` documentation therefore exists in git but not on the box. Harmless (no code
reads it) but worth naming, because "the droplet documents the variable" would be false.

## `tar -tf` keeps the trailing slash that python `tarfile` strips

`git archive` emits directory entries. Listing with the **`tar` CLI** shows them as `grant_watch/`
etc., so an archive of 5 files lists **8** members and a naive count check fails closed —
the inverse of the trap in [[macos-archive-safety]], where `tarfile` strips the slash and a `/$`
filter matches nothing. Filter with `grep -v '/$'` before comparing member sets, and compare the
SET, not just the count.

Related: [[deploy-f894801-announce]], [[deploy-mechanism]], [[restart-means-relaunch]],
[[capability-nudges-sort-last]], [[nudge-variant-ab-is-inert]], [[ssh-rate-limit-and-stdin-traps]],
[[prod-config-audit-20260809]], [[roster-deploy-4c6a543]].
