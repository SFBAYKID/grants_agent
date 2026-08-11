---
name: deploy-0f62485-nudge-followups
description: CURRENT PROD 2026-08-10 — nudge follow-up subsystem live at 0f62485, schema 39, PID 65500; the nudge cron ground truth; and a dry-run that lied when given no Slack client
metadata:
  type: project
---

**PRODUCTION IS `0f6248582cd8f69e2e54c0645224b5ef35a3f0c0`, schema 39, listener PID 65500**
(was `1ffe7ce`, PID 60352). Deployed 2026-08-10 ~18:39 PT. Outage ~2.5s. 11 files
(7 mods + 4 adds: `nudge_sources.py`, `nudge_promises.py`, `nudge_silence.py`,
`tests/test_nudge_followups.py`). `.env` sha `9b68bc18…` and crontab sha `34002d4b…`
byte-identical before AND after. Zero new tracebacks in the fresh `bot.log` region.

## HASH-PINNING CAUGHT A MID-FLIGHT COMMIT FOR THE THIRD TIME

The deploy was authorised for `c2a4e47`. **During preflight, `HEAD` moved to `1b1af6b`** —
someone committed, fast-forwarded `main`, and pushed while I was reading the droplet. The
`[ "$HEADSHA" = "$TARGET" ]` guard in the rsync script refused, which is the only reason it
surfaced. Update the count in [[deploy-mechanism]]: **three times**, not twice.

**The guard must live in the deploy script, not in the operator's head.** Fail closed on
`HEAD != TARGET`, on a dirty tree, and on `merge-base --is-ancestor <target> origin/main`.

## THE NEAR-MISS THAT MADE STOPPING CORRECT

`c2a4e47` would have shipped a **`nudge --dry-run` that silently hid every escalation**:

- `cli.py`: `client = None if dry_run else WebClient(...)`
- `nudges.py:218` — `card_escalated`/`offer_unanswered` ∈ `SILENCE_CLAIM_KINDS` → `_silence_reason`
- `nudge_silence.py:44` — `if client is None: return None`
- `nudges.py:275` — `if replied is None: return "could not verify silence"` → **suppressed**

So the preview would have printed "nothing to follow up on" — reassuring, and wrong — and I
would have reported that to Chase as the verification step. `1b1af6b` gives a dry run a real
client and keeps the safety **structural** instead. Generalisable: **a preview that is
"extra safe" by withholding a dependency can become a preview that cannot see the thing you
are previewing.** Fail-closed in a READ path degrades to blindness, not to safety.

## NUDGE CRON — GROUND TRUTH (both written records were wrong)

```
*/15 8-14 * * 1-5 cd ~/grants_agent && .venv/bin/python -m grant_watch.cli nudge --execute >> cron.log 2>&1
```

Last tick **14:45 PT**. Band `NUDGE_BAND_START_PT=08:30` → `NUDGE_BAND_END_PT=14:30`.
Verified empirically over 1,432 drawn slots: latest slot ever drawn is 14:30, **0 unreachable**
— 15 minutes of margin. **THE BAND MUST NEVER OUTRUN THE LAST TICK**: a slot drawn after it
means "never", and every tick just logs `holding for today's slot` while nothing is delivered.

Both prior records were wrong, and this is the durable lesson — **the dangerous value was the
one written in the project's own docs**:
- the code comment claimed `*/30 8-15` (safe, but not the ground)
- CLAUDE.md recorded `15 9,14` → last tick 14:15 → **252/1432 = 17.6% of slots unreachable**,
  and a day drawing a late first slot loses its second outright.
Both corrected in `0f62485`. **Read the crontab; never quote the band from memory.**

## `nudge --dry-run` CAN BE UNINFORMATIVE TWO DIFFERENT WAYS

Neither is a failure — both are gates that short-circuit before the queue is shown:
1. `nudge: skip: outside business hours` — `in_window(now)`, evenings/weekends.
2. `nudge: skip: daily nudge cap reached (2)` — `MAX_NUDGES_PER_DAY=2`, counting rows with
   state IN `('reserved','delivered','unknown')` for that audience **in the Pacific day**.

`--dry-run --force` skips only #1 and the slot hold; it does **not** skip the cap, so on a day
that already delivered 2 it still shows nothing. **To inspect the real queue, run it on a day
with cap headroom.**

**`--dry-run --force` is read-only-SAFE, verified on the deployed bytes** (not assumed):
`nudges.py:518` `if dry_run:` returns before `chat_postMessage` at 541/548, and the permanent
burn at 508 is gated `if not dry_run and reason in PERMANENT_SUPPRESSIONS`. Connection is
`db.connect_readonly()`, so a write would RAISE rather than happen. Proven in practice by a
before/after fingerprint of `.db`/`-wal`/`-shm` + row count: identical. Only `-shm` mtime
advances (shared-memory index touch by a reader) — **sha and size unchanged, that is not a write.**
`--execute` is the one that burns suppressed subjects it walks past.

## A NEW SUBJECT KIND NEEDED NO MIGRATION — CHECK BEFORE ASSUMING IT DOES

`migrations_nudges.py` changed, which looks like a schema change and is not: it only added
`offer_unanswered` to `NUDGE_SUBJECT_KINDS`. That tuple is **validated in Python, and
`followup_nudges` carries NO CHECK constraint on `subject_kind`** (verified via
`sqlite_master`). Contrast `posts.kind` and `proactive_daily_slots.delivery_kind`, which DO
carry CHECKs — for those a new kind is a table rebuild. Always read `sqlite_master` rather
than pattern-matching the filename.

## PROVE THE ANCESTRY GATE DISCRIMINATES, EVERY TIME

My first negative control was **inert**: I tested the old review-branch tip, but `main` had
been fast-forwarded onto it, so it passed and proved nothing. Use a commit that cannot be on
main — a synthetic orphan works and costs nothing:

```bash
ORPHAN=$(git commit-tree "$T^{tree}" -p "$D" -m throwaway)
git merge-base --is-ancestor "$ORPHAN" origin/main && echo "BAD: gate inert" || echo "gate discriminates"
```

## STATE AT HANDOVER

`followup_nudges` **26 rows, unchanged** by the deploy: `capability_now_available` delivered 2,
`card_escalated` suppressed 3, `card_unengaged` suppressed 13, `crm_preview_expired` suppressed 8.
The 2 delivered are today's, both to `C01DGT9D11D` (17:00:04Z = 10:00 PT Kerry; 21:15:02Z =
14:15 PT Jocelyn) — which is exactly why the forced preview reported the cap.
`offer_unanswered` has **0 rows — the escalation path has never produced a candidate and is
`needs-testing`.** The first real evaluation is the Tue 2026-08-11 08:00–08:30 PT cron ticks.

Rollback artifacts, stamp `20260811T012918Z` (kept): `~/grant_watch.db.bak.<stamp>` (+`-wal`/
`-shm`, `integrity_check: ok` on the COPY), `~/.deployed_revision.bak.<stamp>`,
`~/crontab.backup.<stamp>`, `~/pre-c2a4e47-overwritten.<stamp>.tar.gz` (7 members). Named for
`c2a4e47` because they were taken for that attempt; the pre-image is identical since production
never left `1ffe7ce` in between. Deliberately **no `.env` copy** — 40 credential-bearing copies
already exist; sha256 comparison only. See [[env-credential-sprawl]].

Pre-existing and NOT caused by this deploy: 12 `CompletedPaidCall` tracebacks in `find_contact`
re-enrichment, all below `bot.log` line 1028.
