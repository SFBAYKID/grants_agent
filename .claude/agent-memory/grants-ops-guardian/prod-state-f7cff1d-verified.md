---
name: prod-state-f7cff1d-verified
description: CURRENT PROD 2026-08-10 22:30 PT — f7cff1d, schema 39, PID 68476; full read-only verification recipe and the manifest-diff noise floor (3 stray + 3 mismatched files that are ALWAYS benign)
metadata:
  type: project
---

Measured 2026-08-10 22:31–22:34 PT, read-only, zero writes. Supersedes the "CURRENT PROD"
pointer previously on [[deploy-c7d0d54-accusation-guards]] (`43e6f1d`, PID 67672) — a
later deploy landed at **21:10:39 PT** and the index was stale within hours. **Never
answer "what is running" from the index; read `.deployed_revision`.**

| Fact | Value |
|---|---|
| `.deployed_revision` | `f7cff1d3d75d89dd9c65d5e02c82d77e1873f38a` (41 bytes, mtime 2026-08-10 21:10:39 PT) |
| Listener | PID **68476**, `.venv/bin/python -u -m grant_watch.slack.grant`, started Mon Aug 10 21:10:38 PT |
| Schema | **39** (`MAX(schema_migrations.version)`, 39 rows, contiguous) |
| `followup_nudges` | **26** rows — 2 `delivered`, 24 `suppressed` (all `suppress_reason='stale'`) |
| Crontab | **25** lines, sha256 `34002d4bc67e21f5…` |
| `bot.log` | 1040 lines, 13 tracebacks, all at line ≤870 |
| `cron.log` | 19193 lines |

## The manifest diff has a known, permanent noise floor — learn it or you will re-panic

Comparing droplet file shas against `git cat-file blob <rev>:<path>` is the right way to
prove the revision stamp is truthful, but **six paths always disagree and none of them are
production code.** Budget for them:

**3 present on the droplet, absent from git — all correct:**
- `run_bot.sh` — the bot manager is **deliberately droplet-only and untracked**. This is
  the file a full-tree rsync deletes ([[deploy-beb0520-nudge-force]]).
- `secrets/google_sheets_sa.json` — gitignored credential, droplet-only by design.
- `.pytest_cache/README.md` — tool artifact.

**3 present both sides with different content — all non-runtime:**
- `.claude/agent-memory/architectural-critic/MEMORY.md`
- `.claude/agents/grants-ops-guardian.md`
- `.codex/config.toml`

**~118 tracked paths are "missing" on the droplet** — every one under
`.claude/agent-memory/**`. Agent memory is laptop-side and is never in the `--files-from`
list. Expected, not drift.

**The signal, once the floor is subtracted:** `grant_watch/` **124/124 byte-identical**,
`tests/` 105/105, `docs/` 20/20, `config/` 1/1, `deploy/` 1/1, plus `CLAUDE.md`,
`AGENTS.md`, `architectural.md`, both `requirements*.txt`. Nothing stale under
`grant_watch/` from an earlier deploy. Only **2 files** had mtimes newer than
`.deployed_revision`: `bot.log` and `cron.log`. That is what a clean tree looks like here.

Gotcha in the compare script: after `join`, the line is `path\tsha\tsha`, so
`grep '\.py$'` matches **nothing** and silently reports `compared=0`. Filter on field 1
(`awk -F'\t' '$1 ~ /\.py$/'`) or compare per-directory prefix instead.

## Proactive follow-ups: 2 REALLY delivered, and both are `capability_now_available`

The long-standing "no proactive follow-up has ever been delivered" note is **retired**.
Both delivered rows carry a real `slack_ts` and a non-null `delivered_at`:

- id `1c1bb614…` subject 1 → `U01E908206M` (Kerry), variant **a**, delivered
  2026-08-10T17:00:04Z = **10:00 PT**, `engaged_at` 17:03:45Z — **she replied in 3m41s.**
  That is the A/B ledger's first real engagement datapoint.
- id `c05e2809…` subject 3 → `U06RXJKRXSR`, variant **b**, delivered 21:15:02Z = 14:15 PT,
  `engaged_at` NULL.

The 24 suppressed are all `stale`, split `card_unengaged` 13 / `crm_preview_expired` 8 /
`card_escalated` 3 — the permanent burn described in [[nudge-queue-state-20260809]],
now visible as data.

**`followup_nudges` has no `slack_user_id` column — the recipient is `target_slack`.**
Querying the wrong name raises `no such column` (loud), unlike the `dict(Row).get()` trap
in [[row-get-wrong-column-false-null]] which returns a silent false NULL.

## Reading the nudge cron's own words

Ground-truth line 12: `*/15 8-14 * * 1-5 cd ~/grants_agent && .venv/bin/python -m
grant_watch.cli nudge --execute >> cron.log 2>&1`. Its three log vocabularies, all seen
in one day, and only the second is a delivery:

- `nudge: skip: holding for today's HH:MM PT slot` — the tick fired, the slot had not
  arrived. The overwhelming majority of lines.
- `nudge: nudged <subject_kind> in <channel>` — an actual send.
- `nudge: skip: daily nudge cap reached (2)` — **`(2)` is the CAP, not the count**, the
  same footgun as the drip's `(1)` in [[drip-pacing-and-cap]].

Because the schedule stops at `14`, **no nudge tick exists after 14:45 PT** — an evening
question about "did the nudge run tonight" is answered by the schedule, not the log.

Related: [[deploy-mechanism]], [[tenant-and-layout]], [[verify-the-premise-not-the-claim]],
[[readonly-db-forensics-recipe]], [[ssh-rate-limit-and-stdin-traps]].
