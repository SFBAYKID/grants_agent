---
name: deploy-b42b015-rep-timezone
description: Deploy e905cc2→b42b015 on 2026-08-09 (CURRENT PROD) — code-only, schema stayed 37, PID 31228, 0.35s outage; a nudge now refuses outside 08:00-18:00 in the MENTIONED rep's own zone, and --force does NOT skip it
metadata:
  type: project
---

**LIVE 2026-08-10T03:39:07Z (droplet Sun 20:39:07 PT).** `e905cc21fc437af9f59511ffb6ec49f6c618e496`
→ `b42b0154f32055e083cc9abd48034c11a969a96a` (2 commits: the feature + its doc).
**CODE-ONLY — schema stayed 37**, proven by hash before shipping: `grant_watch/migrations.py` is
byte-identical at both revisions (blob `0130bab1b7ea137e1f73a97e5414d78413be7d72`), so "no migration"
was a fact, not a reading of the diff. Postflight `schema_migrations` MAX **37**, count 37, 46 tables.

Delta 9 paths = **6 deployable** + 3 `.claude/agent-memory/**` (never deployed). 5 modifications
(`CLAUDE.md`, `config/reps.json`, `grant_watch/roster.py`, `grant_watch/slack/nudges.py`,
`tests/test_nudges.py`) + **1 add** (`tests/test_nudge_variants.py`, proven absent beforehand).
Pre-image: all 5 droplet hashes matched the `e905cc2` blobs exactly ⇒ clean base, zero drift.
Archive `a60a6757…4fec`, 153,600 B, member set asserted EQUAL to the intended delta (dir entries
filtered) and each blob hashed against `git show b42b015:<path>`. Remote upload verified byte-exact
(`wc -c` + sha in a SEPARATE session). Staged → `rsync -cai --no-times --no-perms`, itemize exactly
5 `>fcsT......` + 1 `>f+++++++++`, **0 deleting**, catch-all empty, `find -cnewer` exactly the 6,
second pass **0 lines**. All 6 post-image hashes == the `b42b015` blobs.

**No `--delete`, not even in the preview.** The staging dir holds 6 files; `--delete` against the live
tree would delete everything else. The "preview deletions then omit" habit from full-tree days is
actively wrong for the staging-dir shape — cf. [[deploy-a718066-mobile-phone]].

Import closure **115 modules, 0 failures**. `.env` (`9b68bc18…c634`, 67 lines / 33 keys), crontab
(`63495d44…a7f7`, 12 lines) and `run_bot.sh` (`07773019…06bb`) byte-identical before and after —
neither was touched, and neither needed to be. Restart per [[restart-means-relaunch]]: OLDPID
**30759** (== the PID [[deploy-e905cc2-nudge-audience]] recorded ⇒ no out-of-band restart) →
**NEWPID 31228**, **0.35 s outage**, both boot lines, TRACEBACKS 0, PID_COUNT 1, 53 venv maps.
Postflight `integrity_check` ok, `foreign_key_check` exactly the two approved orphans (10642, 11892),
leads 10715, **`followup_nudges` still 0**. Disk 67%, 16 G.

Rollback artifacts (700/600) — `~/backups/deploy-b42b015-20260810T033708Z/`:
- `grant_watch.db.vacuum` 25,096,192 B sha `0bec2cf8244c97642a48688fa53e8dcf9c8b851e08e07070e459e0577ac6c986`
- `code_at_e905cc2.tar.gz` 44,824 B sha `dfac2000c41700605d4d90e185cc864958febd1e8a06acf9e8b65e1500621f99`, 5 members, `gzip -t` OK
- `env.bak`, `crontab.bak`, `deployed_revision.bak`
The vacuum sha is **identical to the previous two deploys'** — the same cheap "zero DB content change
in between" corroboration, holding for a third time. Rollback = restore the tar, `rm`
`tests/test_nudge_variants.py`, re-stamp `e905cc2`, restart. A DB rollback is free here (nothing wrote).

## The gate, measured on the DEPLOYED bytes

`pacing_reason` gained a branch **above** the daily-cap check (asserted by source index, not by
reading the diff):

```python
local = _target_local_hour(candidate, now)
if local is not None and not 8 <= local < 18:
    return f"outside {candidate.target_slack}'s working hours"
```

**`--force` provably does not cover it.** `inspect.getsource(pacing_reason)` on the droplet contains
exactly one `force` guard — `if not force and not in_window(now):` — and the new branch has no force
condition at all. Verified structurally, on the running file, not from the commit message.

Head candidate both instants: `capability_now_available` id **1**, audience `C01DGT9D11D`,
`target_slack=U01E908206M` (Kerry), `priority_at` 2026-07-23 (the date she asked; the `d050c8e`
fix in [[capability-nudges-sort-last]] is why it is at the head at all).

| instant | PT / ET | `_target_local_hour` | `pacing_reason(force=True)` |
|---|---|---|---|
| 2026-08-10T03:23:00Z | Sun 20:23 / 23:23 | 23 | `"outside U01E908206M's working hours"` |
| 2026-08-10T16:15:00Z | Mon 09:15 / 12:15 | 12 | `''` |

The real CLI agrees with the walk: `nudge --dry-run --force --audience C01DGT9D11D` returns
**`nudge: skip: outside U01E908206M's working hours`**. That is the refusal firing on the actual
command path, which a hand-built candidate alone would not prove.

**Fallback is one-sided, as designed.** `_target_local_hour` returns `None` for an empty
`target_slack` (channel cards) and for any rep with no zone, so the gate cannot fire for them. Of 44
due candidates, only the 6 targeting Kerry have a local hour; the other 38 read `None`. `reps.json`
carries `timezone` on **exactly one row** — `U01E908206M` / `America/New_York` — and Chase, Brett,
Anthony, Nelly and Jocelyn have no key at all.

## Monday 09:15 PT WILL deliver, and what it costs

Full chain at `2026-08-10T16:15:00Z` for the head: `suppress_reason=''`,
`pacing_reason(force=False)=''`, `in_window=True`, and `run(dry_run=True, force=False)` renders the
real message to Kerry. Cron line present and correct: `15 9,14 * * 1-5 … cli nudge --execute`, and
the droplet's TZ is `America/Los_Angeles`, so 09:15 there **is** 16:15 UTC.

**That first tick permanently retires 24 subjects.** `run()` walks in `priority_at` order writing
`_record(state='suppressed')` for every `PERMANENT_SUPPRESSIONS` reason
(`stale`, `resolved_since_queued`, `lead_parked`, `engaged_since_queued`) until it reaches the first
sendable one — here indices 0-23, all `stale`, then it stops at #24. Measured, not counted by eye;
an earlier by-hand count said 25 because it included a stale subject sitting *behind* the head, which
`run()` never reaches. Count by breaking at the first non-suppressed candidate.

The 14:15 PT tick then falls to the next capability ask, `U06RXJKRXSR` — a **different** person, and
one with no recorded zone, so the new gate does not apply to it. 14:15 PT is 17:15 ET anyway, still
inside Kerry's window. So the gate never fires on either armed cron time; it exists for forced and
off-hours runs.

Related: [[deploy-mechanism]], [[restart-means-relaunch]], [[nudge-queue-state-20260809]],
[[oneoff-scripts-need-load-dotenv]], [[ssh-rate-limit-and-stdin-traps]].
