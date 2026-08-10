---
name: backups-retention
description: Measured inventory of backup material in the grants tenant home (2.5G total, ~2.2G is backups) and the retention rule proposed to Chase on 2026-08-10 — nothing deleted yet
metadata:
  type: project
---

**Measured read-only 2026-08-10. NOTHING WAS DELETED — this is a proposal awaiting
Chase's decision.**

## What is actually there

Tenant home **2.5 G**; the live checkout `~/grants_agent` is only **286 M**. So roughly
**2.2 G is backup material.**

| class | size | count | note |
|---|---|---|---|
| `.grants_agent.previous.*` | **1.05 G** | 28 | the RETIRED `cp -a` recipe |
| — of which carry a `.venv` | 551 M | **2** | `pre-264e0a7b…` 285 M, `pre-bdea1cd3…` 266 M |
| — venv-less remainder | ~500 M | 26 | code-only, no rollback value git lacks |
| `~/backups/` | **541 M** | 95 entries | |
| — `grant_watch.db.vacuum` | 383 M | 16 files, **only 9 distinct** | 6 share sha `0bec2cf8…` |
| — `code_at_*.tar.gz` | 12.6 M | — | cheap, keep |
| loose `grant_watch.db.bak.*` in home | ~197 M | 17 | pre-dates `~/backups/` |
| `grants_agent_ops_backups` | 20 M | — | |

## Proposed rule

1. **Deploy dirs: keep the last 5, plus every deploy that carried a MIGRATION, for 30
   days.** Git is the rollback source for code, so the tarballs are cheap; the 25 M
   vacuums are the cost.
2. **Vacuums: one per SCHEMA VERSION, not one per deploy.** A code-only deploy's vacuum
   is byte-identical to its predecessor — 16 files, 9 distinct, ~175 M of exact
   duplication. Reclaims that with zero loss of restore capability.
3. **Delete the 26 venv-less `.grants_agent.previous.*` (~500 M).**
   **KEEP `pre-264e0a7b…` and `pre-bdea1cd3…`** — they are the only record of a working
   dependency set until a `pip freeze` lock is committed ([[deploy-mechanism]]).
4. **Loose `grant_watch.db.bak.*`: keep the newest 2, drop the other 15 (~195 M).**
   Superseded by the vacuum convention.
5. **FIRST, on security grounds rather than space: delete the loose `.env` copies under
   `~/backups/`** — `env_before_lead_enrichment_flag_*`, `env_before_org_lead_flag_*`,
   `env_before_audit_flag_*`, `env_before_opportunity_flag_*`, `env.bak.stage2-*`, plus
   the `env.bak` inside older deploy dirs. Live production credentials should not be
   scattered in ~10 files across the filesystem. This is why current deploys take **no**
   `env.bak` when `.env` is untouched.

Total reclaimable ≈ **870 M** while keeping the last 5 deploys, every migration restore
point, and both dependency venvs.

**Not urgent on space** — `/` is 68% with 16 G free, and the disk is shared, so the
grants tenant is not the constraint. The `.env` copies are the part worth doing soon.

Related: [[deploy-mechanism]], [[disk-footprint-and-cruft]], [[deploy-76473e5-user-memory]].
