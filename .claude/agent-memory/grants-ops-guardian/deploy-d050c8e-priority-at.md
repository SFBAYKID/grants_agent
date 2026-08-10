---
name: deploy-d050c8e-priority-at
description: Deploy a718066→d050c8e on 2026-08-09 (SUPERSEDED by 65f05c7) — code-only, schema stayed 37, PID 27714, 0.21s outage; the priority_at fix MOVED capability asks from eligible 14-18 to 0-4; its two fill-leads traps are now FIXED
metadata:
  type: project
---

**LIVE 2026-08-10T02:06:13Z (droplet Sun 19:06 PT).** Production moved
`a7180669f670e756a7424980a51cdec851a0a262` → `d050c8e4f225b4ac4f2894bbeb22990968af3b73`
(3 commits). **CODE-ONLY — schema stayed 37.** Listener **26876 → 27714** (OLDPID == the
PID [[deploy-a718066-mobile-phone]] recorded ⇒ no out-of-band restart). Outage **0.21 s**
(T0 02:06:12.929 → new PID 02:06:13.137). Fresh log region exactly 2 lines, 0 tracebacks,
PID_COUNT 1, uid 1001, 53 venv maps.

## Shape

Delta 13 paths = **7 deployable** + 6 `.claude/agent-memory/**` (never deployed).
6 modifications + **1 add** (`grant_watch/salesforce_lead_fill.py`, proven absent
beforehand). Archive `c327b72a…1a28`, 225,280 B; member set == the intended 7 exactly and
every member hashed against `git show "d050c8e:<path>"` before leaving the laptop.
**Pre-image: all 6 overwritten files hashed byte-exact at a718066.** Staged → droplet-local
`rsync -cai --no-times --no-perms`, itemize 6 `>fcsT` + 1 `>f+++++++++`, 0 deleting,
catch-all empty, `find -cnewer` exactly 7, second dry run empty. Import closure **113
modules, 0 failures** (112 before + the new module).

## No-migration proof, two independent ways

`git diff a718066..d050c8e -- grant_watch/migrations.py` was **0 bytes**, and on the
deployed bytes `len(MIGRATIONS)==34`, `max(version)==37`, `any(version==38) is False`.
Post-deploy `schema_migrations` MAX still 37, integrity ok, FK still EXACTLY the two
approved orphans (10642, 11892). **`MAX_REGISTERED_VERSION` is NOT an attribute of
`grant_watch.migrations`** — reaching for it raised `AttributeError` and aborted the whole
closure script mid-run. Ask for `max(x.version for x in M.MIGRATIONS)` instead.

## THE ORDERING FIX WORKS — measured, not assumed

[[capability-nudges-sort-last]] recorded capability asks at **eligible positions 14-18 of
19**. On the deployed d050c8e bytes, immediately after arming, the same five sit at
**eligible positions 0-4**, and `ELIGIBLE_AHEAD_OF_FIRST_CAPABILITY` went **14 → 0**.

Why it works here: all five asks were made **23-24 July**, while the oldest *non-stale*
other subject stalled **27 July**. `priority_at` reads `observed["asked_at_iso"]` and falls
back to `stalled_at` for every other kind, so the July questions now outrank the August
cards. Staleness still measures from `available_since`, so a freshly armed ask is nowhere
near `DROP_AFTER` — the two clocks are genuinely separate.

**The full due queue is 44 but only 19 are eligible; positions 0-23 are all `stale`.**
Do not read a raw `candidates()` index as a delivery position — 24 of the first 30 are
suppressed. Always render the queue with `suppress_reason()` applied, exactly as `run()`
does, or you will report a capability ask as "24th" when it is first.

`nudge --dry-run --force` now names **Kerry (U01E908206M)**, variant (a), quoting her
23 July "Email those to kerry@monarchconnected.com" back to her. That retires the warning
in [[nudge-queue-state-20260809]] that the head of the queue was channel-only — the head is
now a named person, so **`--force` is no longer a "cannot ping anyone" operation.**

## Arming is inert, and that is provable

`capability_asks.mark_available` is ONE `UPDATE capability_asks SET available_since=?
WHERE capability=? AND state='open' AND available_since IS NULL` + commit. There is no
Slack client anywhere on that path. All four declared (email_results 1, campaign_load 2,
contact_supplied 1, reminders 1 = **5 of 5 armed**) and `followup_nudges` stayed **0 rows**.
The no-`--execute` form uses `connect_readonly()` and returns before `mark_available`,
printing the exact reopen count — DB mtime+size identical across all four previews.

## `fill-leads` — 22 rows, and two things to know before `--execute`

Dry run lists **22 (lead → Salesforce Lead) pairs over 21 DISTINCT leads**;
`crm_action_items.salesforce_id` IS populated (37 non-empty: 36 `00Q` Lead + 1 `00T` Task,
correctly excluded by the `LIKE '00Q%'` filter; 0 have a NULL `lead_id`).

1. **`linked_leads` is DISTINCT on the PAIR, not the lead.** Lead **#231 (Birmingham
   Community Charter HS) maps to TWO Salesforce records** (`00QVC00000Y3mFp2AJ` and
   `00QUZ00000byrvN2AQ`) and appears twice, so the same values would be written to both.
   `--limit N` bounds ROWS, not distinct leads.
2. **`Title` can come from a `linkedin_only` contact.** `proposed_fields` prefers
   `verified`, then `_best_linkedin_contact`, then `vendor_licensed`. Leads #233, #1361,
   #4456, #7845, #3485 have only `linkedin_only`/`not_found` contacts and are still offered
   a **Title** — a LinkedIn *claim* written into Salesforce with no provenance marker.
   No `Email` was offered from a LinkedIn-only lead in this data (they carry none), so the
   exposure today is title-only, but the code does not prevent the email case.

The dry run makes **zero Salesforce calls** — `SalesforceCampaignGateway()` is only
constructed on the `--execute` branch — and uses `connect_readonly()`. DB stat identical.

**BOTH TRAPS ARE NOW FIXED — shipped in `8976530`, deployed in `65f05c7`; see
[[deploy-65f05c7-fill-leads-fix]] for the live proof.** The record below is the pre-fix
measurement and the reason the fix exists; do not re-report it as current.

**BOTH TRAPS RE-CONFIRMED STILL LIVE on 2026-08-09 evening** (a later brief asserted they
were "both fixed in `d050c8e`" — they are not; they landed in `8976530`, which is AFTER
`d050c8e`). `fill-leads --limit 5` preview: lead
**#231 appears twice** (`00QVC00000Y3mFp2AJ` + `00QUZ00000byrvN2AQ`), and lead **#233**
resolves `Title=`**`'Retired Coordinator of Public Relations ...'`** via
`_best_linkedin_contact` → Francisco Mata, `linkedin_claimed` — a RETIRED person's
LinkedIn title, truncation marker included, headed for a live CRM `Title`. #233's other
candidate is titled `'LinkedIn Top Voice'`, which is a badge, not a job.
`--limit 5` bounded 5 ROWS over **4 distinct leads**.
`fill_lead_blanks` genuinely GETs the record and PATCHes only blank fields (source-verified)
— which protects nothing here, because an **empty** `Title` is exactly what it fills.

## Postflight fingerprints

`.env` byte-identical `9b68bc18…c634`, 67 lines / 33 keys, **`sh -n .env` exit 0 with ZERO
bytes of output**, and both `RESEND_API_KEY` + `RESEND_FROM_EMAIL` present AND non-empty in
`/proc/27714/environ`. Crontab byte-identical `63495d44…a7f7`, 12 lines, nudge line intact:
`15 9,14 * * 1-5 … nudge --execute` (09:15 + 14:15 PT weekdays). `run_bot.sh` `07773019…06bb`
unchanged. Disk 67%, 16 G free.

## Rollback artifacts (700/600)

`~/backups/deploy-d050c8e-20260810T020353Z/`:
- `grant_watch.db.vacuum` 25,096,192 B sha `cbc33717118747b1a2dc39ea3ad8e8cd8b29030dfd596dd94a4660dd97b0743d`
  (COPY verified: integrity ok, schema 37, 46 tables, leads 10715, contacts 85, capability_asks 5,
  followup_nudges 0, same 2 FK orphans)
- `code_at_a718066.tar.gz` 58,905 B sha `f921624c5202810f3adac5046a4fc04c9182eaed411719bde4c0c2e4100c95e7`,
  6 members, `gzip -t` OK
- `env.bak` (== `9b68bc18…c634`), `crontab.bak` (== `63495d44…a7f7`), `deployed_revision.bak`

Rollback = restore the tar, `rm grant_watch/salesforce_lead_fill.py`, re-stamp a718066,
restart. **The DB copy pre-dates the arming**, so restoring it would un-arm all five asks;
code rollback needs no DB rollback (this deploy added no migration).

## Two false-pass traps, both live this session

- **macOS bash 3.2 has no `declare -A`.** The pre-image comparison loop died on the
  declaration and still printed `PREIMAGE_FAIL=0` — a clean pass from a loop that never
  ran. Same family as the "0-files-compared PASS". **Always print a CHECKED counter and
  fail closed unless it equals the expected count.**
- **`ssh -n … 'bash -s' <<HEREDOC` again produced total silence and exit 0**, third guise,
  after [[ssh-rate-limit-and-stdin-traps]] already recorded it twice. The whole post-deploy
  verification block ran nothing. The rule holds with no carve-out: no `-n` on the ssh that
  IS the stdin consumer.

Related: [[deploy-mechanism]], [[capability-nudges-sort-last]], [[nudge-queue-state-20260809]],
[[nudge-variant-ab-is-inert]], [[ssh-rate-limit-and-stdin-traps]].
