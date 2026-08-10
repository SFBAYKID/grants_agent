---
name: deploy-e905cc2-nudge-audience
description: Deploy 0716a17→e905cc2 on 2026-08-09 (CURRENT PROD) — code-only, schema stayed 37, PID 30759, 1s outage; adds `nudge --audience` which cuts a forced run's permanent burn from 24 to 3
metadata:
  type: project
---

**LIVE 2026-08-10T03:21:52Z (droplet Sun 20:21:52 PT).** `0716a177b3c8c942f0ed468c698d5e8c372671d2`
→ `e905cc21fc437af9f59511ffb6ec49f6c618e496` (1 commit). **CODE-ONLY — schema stayed 37**:
`migrations.py` is byte-identical at both revs (sha `cae89772…8622`), so "no migration" was proven
by hash, not by reading a diff as empty.

Delta 10 paths = **6 deployable** + 4 `.claude/agent-memory/**`. All 6 modifications, **0 adds,
0 deletes** — which is what justifies omitting `--delete`, never a preview.
Pre-image: all 6 droplet hashes matched the `0716a17` blobs exactly ⇒ clean base, zero drift.
Import closure **114 modules, 0 failures**. Second dry run empty. `.env` (`9b68bc18…c634`, 67 lines /
33 keys), crontab (`63495d44…a7f7`, 12 lines) and `run_bot.sh` (`07773019…06bb`) all byte-identical
before and after. Restart: OLDPID 30207 (== the PID [[deploy-0716a17-org-profile-gate]] recorded ⇒ no
out-of-band restart) → **NEWPID 30759**, **1 s outage**, both boot lines, TRACEBACKS 0, PID_COUNT 1,
53 venv maps. Postflight: schema 37, 46 tables, integrity ok, FK exactly the two approved orphans
(10642, 11892), leads 10715, contacts 85, **`followup_nudges` 0**. Disk 67%, 16 G.

Rollback artifacts (700/600) — `~/backups/deploy-e905cc2-20260810T031838Z/`:
- `grant_watch.db.vacuum` 25,096,192 B sha `0bec2cf8244c97642a48688fa53e8dcf9c8b851e08e07070e459e0577ac6c986`
  (COPY verified: integrity ok, schema 37, 46 tables, leads 10715, nudges 0)
- `code_at_0716a17.tar.gz` 46,792 B sha `ef35835722ac90923fb185892eb4538eb144c724aa205b4b903e16f2dd5676de`, 6 members, `gzip -t` OK
- `deployed_revision.bak`, `env.bak`, `crontab.bak`

**The vacuum sha is IDENTICAL to the previous deploy's** — same cheap "zero DB writes in between"
proof recorded last time, and it held again.

## What the commit does, verified on the deployed bytes rather than believed

`nudge --audience <channel>` filters `run()`'s candidate loop. The load-bearing detail is *where* the
filter sits: `if audience and candidate.audience != audience: continue` is the **first statement in
the loop body**, above the `already` lookup, above `suppress_reason`, and above
`_record(state='suppressed')`. So an out-of-scope subject is skipped with **no ledger row** and a
scoped run genuinely cannot retire a subject in another channel. Asserted on the droplet with
`inspect.getsource(...).index(filter) < .index("_record")`, not by reading the commit message.

`choose_phone` now returns `("", "")` when `org_profile_status != "found"`, gating the `org_phone`
fallback — the second surface of the `cde.ca.gov` defect in [[fill-leads-org-website-laundering]].

## THE MEASUREMENT THAT MATTERS: `--audience` cuts the permanent burn 24 → 3

Measured read-only at 2026-08-09 20:23 PT, replicating `run()`'s exact walk:

| run shape | out-of-scope skipped (no ledger row) | PERMANENTLY retired `stale` |
|---|---|---|
| `--audience C01DGT9D11D` | 21 | **3** |
| unscoped (what Monday's cron does) | 0 | **24** |

44 due candidates: `C01DGT9D11D` 23 / `C0B02721MNK` 18 / `U01DFJWQQJ3` 3 (a DM audience — a THIRD
audience kind now exists; earlier notes only knew the two channels).

**The brief asked me to "confirm it matches your earlier figure of 25." It does not, twice over:** the
unscoped figure has drifted 25 → 24 since 19:20, and the scoped run — the one actually proposed —
burns **3**. Recomputing a "just confirm" number is worth the two minutes every time; see
[[verify-the-premise-not-the-claim]].

Related: [[deploy-mechanism]], [[restart-means-relaunch]], [[nudge-queue-state-20260809]],
[[relayed-consent-is-not-consent]], [[ssh-rate-limit-and-stdin-traps]].
