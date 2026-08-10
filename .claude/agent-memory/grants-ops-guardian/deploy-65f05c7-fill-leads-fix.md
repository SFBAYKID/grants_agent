---
name: deploy-65f05c7-fill-leads-fix
description: Deploy d050c8e→65f05c7 on 2026-08-09 (CURRENT PROD) — code-only, schema stayed 37, PID 29164, 0.30s outage; BOTH fill-leads defects proven FIXED on live data, and the nudge queue proven unchanged by the nudges.py module split
metadata:
  type: project
---

**LIVE 2026-08-10T02:39:36Z (droplet Sun 19:39 PT).** Production moved
`d050c8e4f225b4ac4f2894bbeb22990968af3b73` → `65f05c72894f7d442d783396a2c6d4c3fe9a402d`
(3 commits). **CODE-ONLY — schema stayed 37.** Listener **27714 → 29164** (OLDPID == the PID
[[deploy-d050c8e-priority-at]] recorded ⇒ no out-of-band restart). Outage **0.30 s**
(T0 02:39:36.644 → new PID 02:39:36.941). Fresh log region exactly 2 lines, 0 tracebacks,
0 errors, PID_COUNT 1, uid 1001, 53 venv maps.

## Shape

Delta 8 paths = **5 deployable** + 3 `.claude/agent-memory/**` (never deployed).
4 modifications + **1 add** (`grant_watch/slack/nudge_messages.py`, proven absent beforehand).
Archive `cd803308…5220`, 122,880 B; member set asserted == the intended 5 and every member
hashed against `git show 65f05c7:<path>` before leaving the laptop. **Pre-image: all 4
overwritten files hashed byte-exact at d050c8e.** Staged → droplet-local
`rsync -cai --no-times --no-perms`, itemize 4 `>fcsT` + 1 `>f+++++++++`, 0 deleting,
catch-all empty, `find -cnewer` exactly 5, second dry run empty. Import closure
**114 modules, 0 failures** (113 before + the new module). DB mtime `1786327703` **identical
before and after the entire deploy** — this deploy wrote nothing to the database at all.

## `git archive` dir entries: the `/$` filter DOES NOT WORK from Python

[[deploy-d664548-followups-live]] says to filter `git archive`'s parent-directory members by a
trailing `/`. That is true of `tar -t` output but **FALSE of Python's `tarfile`** — it strips
the slash, so `grant_watch`, `grant_watch/slack`, `tests` came back as three "extra members"
and the fail-closed member check aborted. Filter on **`m.isdir()` / `m.isreg()`**, and assert
that the set of members which are *neither* is empty. The abort was correct behaviour, not a
bug — but it costs a round trip if you carry the shell-shaped rule into a Python checker.

## `--delete` is still wrong here, and the reason is worth restating

The staging dir holds only the 5-file delta, so `rsync --delete staging/ live/` would delete
the entire rest of the live tree. The correct justification for omitting it is the git delta
itself: `--name-status` was 4×`M` + 1×`A`, **zero `D`**. Prove "no deletions" from the delta,
never from a `--delete` preview against a partial staging dir.

## BOTH `fill-leads` DEFECTS ARE FIXED — measured on production data

The prior brief claimed these were fixed in `d050c8e`; they were not, they landed in `8976530`.
Now verified on the deployed bytes with `fill-leads --limit 5` (dry run, zero DB writes):

- **Lead #231 appears ONCE** (`00QUZ00000byrvN2AQ`; the lexicographic `MIN` of its two CRM ids,
  the other being `00QVC00000Y3mFp2AJ`). `linked_leads` now does `GROUP BY i.lead_id` +
  `MIN(salesforce_id)`; `SELECT DISTINCT i.lead_id, i.salesforce_id` is gone from the source.
- **`--limit 5` now bounds LEADS**: 5 rows over **5 distinct leads** (#231-#235). It was
  5 rows over 4 distinct leads.
- **Lead #233 is offered NO `Title`** — only `Industry, State, Website`. Its only two contacts
  are `linkedin_only` (`'LinkedIn Top Voice'` and `'Retired Coordinator of Public Relations …'`),
  and `proposed_fields` now allowlists `{verified, vendor_licensed}`. The
  `_best_linkedin_contact` import is gone from `salesforce_lead_fill` entirely.
- **The other side of the claim was checked too, and matters as much:** #232/#234/#235 still get
  `Title`+`Email`, and every one traces to a `contact_status='verified'` row (Superintendent of
  Schools / Superintendent). The fix removed the laundering path without silently gutting the
  feature. Lead #231 now offers no contact fields at all — correct, its only non-`not_found`
  contact is `linkedin_only`.

Verify this class of fix by **walking `contacts_for_lead` alongside `proposed_fields`** and
printing each contact's `contact_status`. The CLI listing alone shows *which* fields are
offered, never *what evidence* backs them.

## The nudges.py split is behaviour-preserving, and that is PROVEN not assumed

`nudges.py` 974 → 774 lines; `nudge_messages.py` 223 lines (the 1,000-line cap).
An AST comparison of every top-level name moved out (`build_message`, `_capability_message`,
`_escalation_message`, `_CAPABILITY_OFFER`, `_CAPABILITY_HEADLINE`) showed the two constants
byte-identical and the three functions differing **only** in the annotation being quoted
(`candidate: NudgeCandidate` → `candidate: "NudgeCandidate"`), required because
`nudge_messages` imports the class under `TYPE_CHECKING` to avoid a cycle. Bodies byte-identical.
Nothing that stayed in `nudges.py` changed at all.

Post-deploy the queue is **identical to the pre-split baseline**: DUE **44**, ELIGIBLE **19**,
25 suppressed `stale`, capability asks at eligible **0-4**, head = **Kerry `U01E908206M`**,
`capability_now_available`, variant (a), audience `C01DGT9D11D`. `nudge-report` → "No follow-ups
have been delivered yet". `followup_nudges` still **0 rows**.

**`nudges.build_message is nudge_messages.build_message` → True** is the cheap check that the
re-export resolves to the NEW file; `hasattr(nudges, '_capability_message')` → False proves the
old copy is really gone. Assert both — the removed-symbol half is what a stale or half-applied
file would still satisfy.

## HEAD OF THE QUEUE NOW MENTIONS A REAL PERSON

`target_slack='U01E908206M'` (Kerry) at eligible #0, and #1-#4 mention `U06RXJKRXSR` and
`U04ASV42UJD`. The first channel-only `card_unengaged` (`target_slack=''`) is now at #5. So the
old "a forced run cannot ping anyone" property is **fully retired** — the first five eligible
subjects all @-mention a colleague in production `C01DGT9D11D`. Monday 09:15 PT is the intended
first delivery; forcing it earlier spends that timing deliberately.

## Postflight fingerprints (all byte-identical to pre-deploy)

`.env` `9b68bc18…c634`, 67 lines / 33 keys, `sh -n .env` exit 0 silent. Crontab
`63495d44…a7f7`, 12 lines, and the nudge line matched with `grep -cxF` (exact whole-line, count
1): `15 9,14 * * 1-5 cd ~/grants_agent && .venv/bin/python -m grant_watch.cli nudge --execute
>> cron.log 2>&1`. `run_bot.sh` `07773019…06bb`. Schema MAX 37, 46 tables, `integrity_check` ok,
`foreign_key_check` EXACTLY the two approved orphans (10642, 11892), leads 10715, contacts 85,
capability_asks 5. Registry: 34 migrations, max 37, `HAS_38=False`. Disk 67%, 16 G.

## Rollback artifacts (700/600)

`~/backups/deploy-65f05c7-20260810T023626Z/`:
- `grant_watch.db.vacuum` 25,096,192 B sha `0bec2cf8244c97642a48688fa53e8dcf9c8b851e08e07070e459e0577ac6c986`
  (COPY verified: integrity ok, schema 37, 46 tables, leads 10715, followup_nudges 0, same 2 FK orphans)
- `code_at_d050c8e.tar.gz` 39,511 B sha `7236be2bd9529b34389089783bb998f0746559fb294c82394c7cff563ddabe56`,
  4 members, `gzip -t` OK
- `env.bak` (== `9b68bc18…c634`), `crontab.bak` (== `63495d44…a7f7`), `deployed_revision.bak` (41 B)

Rollback = restore the tar, `rm grant_watch/slack/nudge_messages.py`, re-stamp d050c8e, restart.
**No DB rollback is needed or wanted** — this deploy wrote nothing; restoring the copy would
only discard unrelated live traffic. `~/backups` is now **301 M**, still no retention policy
([[disk-footprint-and-cruft]]).

## Loose end, NOT touched

`~/grants_agent/deploy_rsync.sh` exists on the droplet at repo root — untracked, not created by
this session, and **not used** (the task forbids it). Probably the other toolchain
([[codex-parallel-writer-forensics]]). Flagged for Chase, left alone.

Related: [[deploy-mechanism]], [[deploy-d050c8e-priority-at]], [[oneoff-scripts-need-load-dotenv]],
[[ssh-rate-limit-and-stdin-traps]], [[nudge-queue-state-20260809]].
