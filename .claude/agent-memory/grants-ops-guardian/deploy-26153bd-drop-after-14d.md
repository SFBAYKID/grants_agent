---
name: deploy-26153bd-drop-after-14d
description: Deploy beb0520→26153bd on 2026-08-09 (CURRENT PROD) — DROP_AFTER 5d→14d, code-only, schema stayed 32, PID 19225; the widening did NOT make the playground reachable and the nudge was NOT fired
metadata:
  type: project
---

**LIVE 2026-08-09T23:11:48Z.** Production moved `beb0520ceebcd5ed9aaeb52c4f7b2371099b34a0` →
`26153bd57d6499baf31fb7208add6df48da07b0d`. Listener **17737 → 19225**. `.env` and crontab
untouched. All `verified`.

**The range is 3 commits, not 2** (the task said 2): `26153bd` (the fix) + `f95db01` + `dc809cd`
(both memory-only chores). Only ONE carries code. Count the range yourself — see
[[deploy-2239a18-human-asserted]] for the same class of stated-vs-actual drift.

**NO MIGRATION — proven.** `git diff --name-status beb0520..26153bd` = 7 paths: `nudges.py` (M),
`tests/test_nudges.py` (M), and 5 `.claude/agent-memory/**` (1 M + 4 A). Zero paths matching
`migrat`. Schema read **32** before and after. The whole code change is one constant plus a comment.

**Shipped 2 files via `--files-from`** (agent-memory is NEVER on the droplet — see
[[roster-deploy-4c6a543]]). Preview 2 changed / 0 adds / 0 deletions / 0 other, real run 2, second
pass **0** (idempotent), `find -newermt '-10 minutes'` exactly 2. Sha ladder checked BOTH ways: live
files hashed identical to `git show beb0520:` before and to `git show 26153bd:` after.

**A restart was NOT actually required** — `import grant_watch.slack.grant` then checking
`sys.modules` proves `grant_watch.slack.nudges` is **not** in the bot's import closure (the nudge
worker is a separate CLI process). Restarted anyway because the operator asked. Cheap check, worth
running before every restart decision.

**Boot takes ~30–45 s, not 6.** At +6 s the new log region was EMPTY — no "Grant is listening", no
"Bolt app is running". That is not a failure, it is impatience; at +65 s both lines were present and
tracebacks 0. Do not conclude a bad boot from a 6-second sample.

## THE FINDING THAT MATTERS: 14 days does NOT reach the playground

The stated purpose was "this also makes PLAYGROUND subjects reachable again, which is the point."
**Measured after the deploy, that is FALSE.** Of 36 due subjects: PRODUCTION 18 due / **14 eligible**,
PLAYGROUND 18 due / **0 eligible — all 18 still `stale`**.

The newest playground subject stalled **2026-07-18T19:00:18Z**, i.e. **22.2 days** old. With
`DROP_AFTER=14d` its `drop_after` was 2026-08-01 — it aged out eight days before the deploy.
Reaching it would need `DROP_AFTER >= 23 days`, and that threshold **grows by one day every day**,
so no fixed constant catches a queue that is no longer being fed. The real unblock is **fresh
playground activity**, not a bigger number. (Playground `crm_campaign_batches` is 0, so batch
subjects can never arise there at all — [[nudge-queue-state-20260809]].)

Because candidates sort `stalled_at` oldest-first, all 18 playground subjects occupy queue indices
**0–17** — they are AHEAD of production in the queue, not behind it, and every one is suppressed.
"How far down the queue" is the wrong mental model here.

**So the nudge was NOT fired.** Eligible #0 was audience `C01DGT9D11D` (PRODUCTION), and the
operator's instruction was playground-only. No `--execute` ever ran; `followup_nudges` is still
**0 rows**.

## Check this BEFORE any future `nudge --execute`

`run()` walks candidates and, for every suppressed one it passes, writes a **permanent** `suppressed`
row (`_record(..., state="suppressed")`) under `POLICY_VERSION='nudge-v1'` — that subject can then
**never** be nudged. Today one `--execute` would have burned **22** subjects that way. That is
harmless when the reason is `stale` (a stale subject is already unreachable forever), but
`suppress_reason` can also return **`channel_guard_active`, which is TRANSIENT** — running while a
channel guard is up would permanently kill every subject in that audience for a temporary outage.
**Always confirm `db.channel_guard(conn, audience) is None` for both audiences first.** Both were
`None` on 2026-08-09.

`--dry-run` never writes (returns before `_record`); proven again by identical DB mtime
(1786314229) and size (26,828,800) across three walks.

**`nudge --dry-run` reports only `subject_kind` + message text — NOT the audience.** You cannot tell
production from playground from the CLI line. Do a `connect_readonly()` walk with the real
`candidates`/`suppress_reason` functions to read `audience` and `target_slack`.

**`load_dotenv()` with no argument CRASHES over `python - <<EOF`** (`find_dotenv()` asserts on
`frame.f_back`). Pass the explicit path: `load_dotenv("/home/grantwatch/grants_agent/.env")`. See
[[oneoff-scripts-need-load-dotenv]].

**Postflight (all verified):** revision stamped 40 B; PID 19225 uid 1001, cwd
`/home/grantwatch/grants_agent`, 53 venv maps, PID_COUNT 1, stable at 223 s; `integrity_check` ok;
`foreign_key_check` exactly the two known orphans (10642, 11892); `.env` sha `f4abd546…2a99` / 66
lines; crontab sha `575fbc7c…1a72` / 10 lines; `run_bot.sh` sha `07773019…06bb`; 0 tracebacks; disk
67% / 17 G free.

**Rollback artifact (retained):** `~/backups/deploy-26153bd-20260809T230906Z/` —
`code_at_beb0520.tar.gz` (8,835 B, sha256 `5cdf255e…652b`, 2 members, `gzip -t` OK) +
`.deployed_revision.bak`. No DB backup needed: nothing migrates and the deploy wrote no rows.
My staging dir was removed afterward.

**A local `chmod +x` on a scratchpad helper was blocked by the permission classifier.** I did not
retry it and did not treat it as a task-wide stop: per [[coordinator-stop-is-stop]] the rule exists
to stop routing around review of a PRODUCTION mutation, and a temp-file permission bit is not that.
I dropped the helper and used the canonical inline ssh command, which is the sanctioned primary
mechanism and was never blocked. A block on the rsync/restart/execute shapes WOULD have been a hard
stop.
