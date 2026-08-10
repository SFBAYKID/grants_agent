---
name: deploy-cdfdaf9-threadscan
description: Deploy 2159d67→cdfdaf9 on 2026-08-10 (CURRENT PROD) — code-only, schema stayed 38, PID 34654, 0.147s outage; cron 13→19 lines (nudge */30 8-15 → */15 8-14, new weekly scan-threads); the nudge job was ALREADY on, and the announcement loader printed "0 new" on the run that DID update
metadata:
  type: project
---

**LIVE 2026-08-10T05:54:59Z (droplet Sun 22:54:59 PT).**
`2159d6758d087b67ea73297688661d486057b7b7` → `cdfdaf917ae752f1de09b2b4b75991e0ed652285`
(5 commits). **Schema stayed 38** — `git diff -- grant_watch/migrations*.py` empty before
shipping, and `SCHEMA_MAX` read 38 before, after the rsync, after the announcement reload
(which uses the MIGRATING `db.connect()`) and after `scan-threads --execute`.
Listener **33390 → 34654**, **0.147 s outage** — the fastest recorded on this box.

## Fingerprints now

Revision `cdfdaf9…2285` (41 B with trailing newline). Schema **38**, `schema_migrations` 38,
**47 tables**, `integrity_check` ok, `foreign_key_check` exactly the two approved orphans
(10642, 11892). leads 10715. `.env` `9b68bc18…c634` / 67 lines / 33 keys / 600 —
**unchanged**, and all 33 keys non-empty. `run_bot.sh` `07773019…06bb`.
crontab `cd38cc6e…6cc5` (13 lines, 1375 B) → **`11b4a201e5b491c07ff7af95acd0e1a0e68cdef544d48dcdcf9f47cb43174964`**
(19 lines, 1941 B). Disk 68%, 16 G. `~/backups` now **469 M** and still has no retention policy.

Delta 14 paths = **11 deployable** + 3 `.claude/agent-memory/**`. 9 mods + **2 adds**
(`grant_watch/thread_scanner.py`, `tests/test_thread_scanner.py`, both proven absent at
2159d67). Pre-image: all 9 mods hashed to the `2159d67` blobs ⇒ clean base, zero drift.
Archive `3626fa61…c428`, 70,571 B, member set asserted EQUAL to the delta, upload byte-exact.
`rsync -cai --no-times --no-perms --files-from` → 9 `>fcsT` + 2 `>f+++`, **0 deleting**,
catch-all empty, second pass 0 lines, `find -cnewer` exactly 11, post-image 11/11 == the
`cdfdaf9` blobs. Import closure **116/116, 0 failures** (was 115 — `thread_scanner` is the
new module; `find grant_watch -name '*.py'` counts 117 because `walk_packages` enumerates
only what is *below* the package).

**Whole-code-closure drift check: 119/119 byte-identical to `cdfdaf9`**, 0 untracked extra
`.py` under `grant_watch`. Post-restart log region = exactly the 2 boot lines, **0
tracebacks**, PID_COUNT 1, 53 venv maps, `status=restart_attempt` in `cron.log` at the
relaunch instant. The 13 `Traceback` lines in `bot.log` are all HISTORICAL — the last sits at
line 870 and my boot line is 998, so **0 after the restart**. Count tracebacks in the FRESH
region, never over the whole file.

Rollback artifacts (700/600) — `~/backups/deploy-cdfdaf9-20260810T055212Z/`:
- `grant_watch.db.vacuum` 25,108,480 B sha `598af5f20f4d0ca657a858f06a999a219450332f842bd2dd8cd387c0b2a011c6`
  (schema 38 copy, integrity ok, 5 capability_asks, 0 followup_nudges)
- `code_at_2159d67.tar.gz` 57,231 B sha `71b9c63343acdbab321d58cf5e693b2ae349a09cd48816b575e48cce1375525e`, 9 members, `gzip -t` OK
- `crontab.bak` (sha == live before the edit), `deployed_revision.bak`. No `env.bak` — `.env` untouched.

## THE BRIEF'S PREMISE WAS FALSE — the nudge job was already in cron

The task said *"The follow-up nudge worker is deliberately NOT in cron today. TURN IT ON."*
It **was already on**, and had been since [[deploy-cadfefe-nudge-slots]]: line 12 read
`*/30 8-15 * * 1-5 … nudge --execute`. The 08:00 announce job was live too. So the end state
Chase asked for already held; what this deploy actually did was **refine the tick
resolution**, not switch anything on. Read the live crontab before believing a claim about
it — [[verify-the-premise-not-the-claim]].

## Cron: 13 → 19 lines

| change | before | after |
|---|---|---|
| nudge | `*/30 8-15 * * 1-5` | **`*/15 8-14 * * 1-5`** |
| scan-threads | *(absent)* | **`40 4 * * 1`** + a 5-line comment |

Measured on the deployed slot logic over 100 weekdays × 2 channels (400 slots), band
08:30–14:30 PT, `MAX_NUDGES_PER_DAY` 2, `MIN_GAP` 4 h:

| grid | ticks/day | unreachable slots | worst wait |
|---|---|---|---|
| `*/30 8-15` (was) | 16 | 0 | 29 min |
| **`*/15 8-14` (now)** | **28** | **0** | **14 min** |
| `*/15 8-15` | 32 | 0 | 14 min |

`*/15 8-15` was rejected: the four ticks after 14:45 are past the band's structural maximum
slot (14:30) and buy nothing. **A finer cron cannot increase volume** — `run()` sends at most
one nudge per invocation and `_sent_today` + the daily cap bound the day; the head is allowed
at 20 of the 28 Monday ticks and still delivers once.

Edit guards (both edits): backup still identical to live; old literal present exactly once;
new literal absent; **exact full-line `awk` substitution** (`$0==old`); every untouched line
re-proven with `grep -qxF`; `diff` counted (1 removed / 5 added, then 4 / 6); and explicit
assertions that the two look-alikes survived — line 4's DISABLED comment carrying `*/30 5-17`
and the live `remind` at `*/30 8-16`. Any regex on `*/30 8-` or `5-17` hits one of them.

## THE SCAN SILENTLY DROPS MOST OF THE CHANNEL — see [[thread-scan-ratelimit-truncation]]

Three runs of `scan-threads` on the same channel within 40 minutes reported **29, 13 and 4**
Grant threads. Not nondeterminism in the model — `conversations.replies` rate-limiting,
swallowed by a bare `except SlackApiError: continue`. Full measurement in its own memory.

## `announce --load` prints "0 new" on the run that DID update

Same family as the `capability-seed` trap in [[deploy-2159d67-resend-test-email]]:
```
announce: 0 new announcement(s) recorded
```
is the output of the **successful** revision. `cmd_announce` prints `added`, which counts only
INSERTs; the new UPDATE branch revises an unposted row and is invisible in the summary.
Verify in the DB: body **809 → 830 chars**, `"From here on"` now present, the unqualified
`"*Following up.* If something we started goes quiet"` gone, `posted_at` still NULL. Also
note `--load` returns **before** any Slack client is constructed, so it can never post
regardless of `--execute`.

## Discovery lands INERT, proven three ways

1. Source: `thread_scanner` never calls `mark_available` and never passes `available_since`.
2. Query: `nudges._capability_asks` filters `WHERE state='open' AND available_since IS NOT NULL`.
3. Data, after `--execute`: `SELECT COUNT(*) … WHERE recorded_by='thread-scan' AND
   available_since IS NOT NULL` = **0**.

`capability_asks` 5 → **20** rows (15 `thread-scan`, 5 `seed:…json`). All 5 pre-existing rows
were ALREADY armed (declared out of band 2026-08-10T02:08 UTC), so "which capabilities are
unarmed" is exactly the 8 new slugs. Caveat worth carrying: `canonical_capability` folds a new
ask onto an existing slug, so a future `capability <slug>` for an already-declared name will
arm every newly discovered row sharing it — declaring is not per-row.

## A bare probe named the wrong colleague — again

My queue walk reported the head as `subject_id=3` / `U06RXJKRXSR` with one ask suppressed
`capability_not_ready`. **That was my probe, not production**: it had no `load_dotenv`, so
`RESEND_API_KEY` was absent. With the env loaded: **44 candidates, 19 eligible, 25 `stale`,
head = `capability_now_available` id 1 → `U01E908206M` (Kerry)**, `pacing_reason` `''`,
`in_window` True at Monday 10:00 PT. Exactly the failure [[oneoff-scripts-need-load-dotenv]]
records, hit anyway. `cli.main()` calls `load_dotenv()` at line 680, so the CRON path is fine
— only hand-written probes are exposed.

Monday 2026-08-10 slots: `C01DGT9D11D` **09:54 + 14:11 PT**, playground 08:32 + 13:51. First
delivery lands on the **10:00 PT** tick.

Related: [[deploy-2159d67-resend-test-email]], [[deploy-cadfefe-nudge-slots]],
[[thread-scan-ratelimit-truncation]], [[restart-means-relaunch]], [[deploy-mechanism]],
[[oneoff-scripts-need-load-dotenv]], [[verify-the-premise-not-the-claim]],
[[ssh-rate-limit-and-stdin-traps]].
