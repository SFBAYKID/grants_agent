---
name: deploy-cadfefe-nudge-slots
description: Deploy b42b015→cadfefe on 2026-08-09 (CURRENT PROD) — code + the coupled crontab change to `*/30 8-15`; schema stayed 37, PID 31756, 0.20s outage; Monday now delivers at 10:00 PT, and the Eastern rep's clock closes the day BEFORE the band does
metadata:
  type: project
---

**LIVE 2026-08-10T03:52:56Z (droplet Sun 20:52:56 PT).** `b42b0154f32055e083cc9abd48034c11a969a96a`
→ `cadfefe1ba0356495a2db16331d45186e1d54874` (1 commit). **The deploy and a crontab change shipped
together, deliberately** — see "why they are coupled" below.

**CODE-ONLY — schema stayed 37**, proven by hash before shipping: `grant_watch/migrations.py` is
byte-identical at both revisions (blob `0130bab1b7ea137e1f73a97e5414d78413be7d72`, the same blob as
the previous two deploys). Postflight `schema_migrations` MAX **37**, count 37, 46 tables.

Delta 5 paths = **3 deployable** + 2 `.claude/agent-memory/**` (never deployed). All 3 are
MODIFICATIONS, zero adds/deletes: `grant_watch/slack/nudges.py`, `tests/test_nudges.py`,
`tests/test_nudge_variants.py`. Pre-image: all 3 droplet hashes matched the `b42b015` blobs exactly
⇒ clean base, zero drift. Archive `13b4d759…745b`, 92,160 B, member set asserted EQUAL to the
intended delta, upload verified byte-exact in a SEPARATE session. Staged → `rsync -cai --no-times
--no-perms --files-from`, itemize exactly 3 `>fcsT......`, **0 deleting**, catch-all empty, second
pass **0 lines**, `find -cnewer` exactly the 3 (+ live `cron.log`). All 3 post-image hashes == the
`cadfefe` blobs. No `--delete`, per [[deploy-b42b015-rep-timezone]].

Import closure **114 submodules, 0 failures**. That is the same tree as the "115" recorded on the
previous deploy — `grant_watch/` holds **115 `.py` files** and `pkgutil.walk_packages` enumerates the
114 *below* `grant_watch`, the 115th being `grant_watch/__init__.py`, the package itself. Not a
missing module; the two numbers count different things.

Restart per [[restart-means-relaunch]]: OLDPID **31228** (== the PID
[[deploy-b42b015-rep-timezone]] recorded ⇒ no out-of-band restart) → **NEWPID 31756**, **0.204 s
outage**, both boot lines, TRACEBACKS 0, PID_COUNT 1, 53 venv maps. `.env` (`9b68bc18…c634`, 67
lines / 33 keys) and `run_bot.sh` (`07773019…06bb`) byte-identical before and after. Postflight
`integrity_check` ok, `foreign_key_check` exactly the two approved orphans (10642, 11892), leads
10715, **`followup_nudges` still 0**. Disk 68%, 16 G.

Rollback artifacts (700/600) — `~/backups/deploy-cadfefe-20260810T034817Z/`:
- `grant_watch.db.vacuum` 25,096,192 B sha `0bec2cf8244c97642a48688fa53e8dcf9c8b851e08e07070e459e0577ac6c986`
- `code_at_b42b015.tar.gz` 22,471 B sha `dc9753e7b6ae972c8e27f6ec18478f490e4e3b610f0f9d54b31ddd7f4f74b7db`, 3 members, `gzip -t` OK
- `env.bak`, `crontab.bak` (proven identical to live before the edit), `deployed_revision.bak`
The vacuum sha is **identical to the previous three deploys'** — a fourth run of the same cheap
"zero DB content change in between" corroboration. Rollback = restore the tar, re-stamp `b42b015`,
restore `crontab.bak`, restart.

## The crontab half, and why it could not ship separately

`15 9,14 * * 1-5 … nudge --execute` → `*/30 8-15 * * 1-5 … nudge --execute`.
crontab `63495d44…a7f7` (12 lines, 1268 B) → **`8b4dd525da47f2d407e18a5f4883ad1cc645182d1580c28c0c940b21714d994c`**
(12 lines, 1270 B). Exactly one line changed; the other **11 proven byte-identical individually**
with `grep -qxF`, not by eye.

Guards that made the edit safe, and the two traps they exist for: **line 4** is the disabled
`salesforce-followups` comment carrying `*/30 5-17`, and **line 11** is the live `remind --execute`
at `*/30 8-16`. Any regex on `*/30 8-` or `5-17` would have hit one of them. The edit was an
**exact full-line `awk` substitution** (`$0==old`), gated on: backup still identical to live, the OLD
literal matching exactly once, the NEW literal absent, line count stable, `diff` showing exactly 1
removed + 1 added and those being the expected literals, all 11 others present verbatim, no line
still carrying `^15 9,14 `, and the `remind` line intact. `diff` was captured once into a variable
(`|| true`) rather than piped, per the pipefail trap in [[tenant-and-layout]].

## The slot mechanism, measured on the DEPLOYED bytes

Band `NUDGE_BAND_START_PT` 08:30 → `NUDGE_BAND_END_PT` 15:00 PT, `MAX_NUDGES_PER_DAY` 2, `MIN_GAP`
4 h. `daily_slots(date, audience)` seeds `random.Random(f"nudge:{date}:{audience}")`, so every tick
of a day agrees. **Always exactly 2 slots, and their ranges are structural, not empirical:**
- slot0 ∈ **[08:30, 11:00]** — `latest = end − remaining×gap = 900 − 240 = 660 min`.
- slot1 ∈ [slot0+4 h, 15:00] ⊆ **[12:30, 15:00]**.

Drawn slots for `C01DGT9D11D`: **Mon 2026-08-10 → 09:40 + 13:45 PT; Tue 08-11 → 09:06 + 13:37;
Wed 08-12 → 09:47 + 13:55.** They differ day to day and all fall at or before 15:30. (Playground
`C0B02721MNK` Monday draws 08:35 + 14:28 — different seed, different day shape.)

**Monday 2026-08-10 delivers at 10:00 PT (17:00Z).** Head at every instant is
`capability_now_available` id **1**, audience `C01DGT9D11D`, `target_slack=U01E908206M` (Kerry), with
24 subjects suppressed ahead of it. `suppress_reason=''` at all four:

| Monday instant | `pacing_reason(force=False)` | `in_window` | delivers |
|---|---|---|---|
| 09:15 PT | `"holding for today's 09:40 PT slot"` | True | no |
| 11:15 PT | `''` | True | **yes** |
| 13:15 PT | `''` | True | **yes** |
| 15:15 PT | `"outside U01E908206M's working hours"` | True | no |

## THE EASTERN REP CLOSES THE DAY BEFORE THE BAND DOES

The band was designed to end at 15:00 with the 15:30 tick "spare". **For an Eastern target there is
no spare tick — and no 15:00 tick either.** Walking the real `*/30 8-15` grid, ticks 08:00–09:30
hold for the slot, **10:00 through 14:30 all deliver**, and **both 15:00 and 15:30 refuse** with
`outside U01E908206M's working hours`: 15:00 PT is 18:00 ET and the gate from
[[deploy-b42b015-rep-timezone]] is `8 <= local < 18`. So this head's effective ceiling is **14:59
PT**, and a slot drawn at exactly 15:00 (the structural max; observed max over 100 weekdays was
14:59) would be unreachable for her. Narrow, but it is the same class of silent hold the cron change
exists to prevent — check the *target's* zone, not just the band, before widening the band again.

## The brief's premise was directionally right and factually wrong — measured, not assumed

The task said the old two-tick cron would have made Monday silent. **It would not have.** Because
slot0 is structurally ≤ 11:00 < 14:15, the first nudge of any day was always eventually reachable;
Monday under the old cron delivers at **14:15 PT** (`pacing_reason` = `''`, measured). What the old
cron actually broke, over 100 sampled weekdays for `C01DGT9D11D`:

| | old `15 9,14` | new `*/30 8-15` |
|---|---|---|
| slot0 with no tick at/after it | 0/100 | 0/100 |
| **slot1 after the last tick ⇒ silent** | **74/100** | **0/100** |
| slot0 shoved off its drawn time to 14:15 | 72/100 | n/a (worst wait 29 min) |
| distinct possible delivery times | **2** | **16** |

So the coupling was genuinely required — for the *second* daily nudge and for the feature's whole
purpose — but not for "Monday delivers at all". This is the nudge instance of the arithmetic in
[[drip-slot-band-vs-cron-granularity]]: *distinct post times ≈ band ÷ cron granularity*. With two
ticks the 6.5-hour band collapsed to two clock times, which is furniture with extra steps.

`nudge --dry-run --force --audience C01DGT9D11D` run verbatim at Sun 20:56 PT returned
**`nudge: skip: outside U01E908206M's working hours`** — `--force` skips the slot hold and the
business-hours window but NOT the recipient's own clock, exactly as designed. Read-only proven by
DB size+mtime identical before and after and `followup_nudges` still 0, not by exit code.

Related: [[deploy-mechanism]], [[restart-means-relaunch]], [[nudge-queue-state-20260809]],
[[oneoff-scripts-need-load-dotenv]], [[ssh-rate-limit-and-stdin-traps]],
[[verify-the-premise-not-the-claim]].
