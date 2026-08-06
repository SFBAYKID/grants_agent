---
name: campaign-writes-flag-armed-in-prod
description: SALESFORCE_CAMPAIGN_WRITES_ENABLED=1 is LIVE in the droplet .env against PRODUCTION Salesforce — the kill switch for the new Campaign-batch write code is ON, not off
metadata:
  type: project
---

**Verified read-only 2026-07-26T01:43Z.** The droplet `.env` carries
`SALESFORCE_CAMPAIGN_WRITES_ENABLED=1` (count=1) and the live bot (pid 623507) has it set in
`/proc/<pid>/environ`. Alongside it: `SALESFORCE_WRITE_EXPECT_SANDBOX=0 ` (note the **trailing
space**; the code compares `== "1"`, so it is falsey either way = no sandbox requirement).
`GRANT_RICH_CARD_ENABLED` is ABSENT from both `.env` and the process environ (rich card OFF — good).

**Why this matters:** the flag was added 2026-07-17 when Salesforce pointed at the `monarchdev`
SANDBOX. Salesforce moved to **PRODUCTION** on 2026-07-20 (CLAUDE.md: IsSandbox=False, Org `…8EAM`).
The flag was never turned back off, so it now arms production. `enrich/salesforce_campaign_policy.py:34
writer_enabled()` is the ONLY reader; `enrich/salesforce_campaigns.py:766` is the ONLY gate, and it
guards `execute_campaign_creation` + `execute_membership`. The `reconcile_*` paths are reached BEFORE
that gate but issue no POST (verified by reading 4a4d550). So with the flag at `1`, the only remaining
barriers to a real production Campaign write are `verify_write_scope` and a human clicking approve.

**Live exposure at the time of measurement:** `pre_migration_state.json` shows **4 `create_campaign`
actions in state `ready`**, all `external_write_started=0` — three in the PRODUCTION channel
`C01DGT9D11D` (thread `1784819113.594459`, requester `U04ASV42UJD`) and one in the playground
`C0B02721MNK`. A ready preview + an armed flag + a human tap = a production Campaign.

**How to apply:** any task whose safety gate reads "verify `SALESFORCE_CAMPAIGN_WRITES_ENABLED` is
absent or 0" **fails closed today** — the precondition is false, and the fix is Chase's call, not the
guardian's. Setting it to `0` is a one-line `.env` edit (use the append/rewrite recipe in
[[tenant-and-layout]]) but it CHANGES PRODUCT BEHAVIOUR (disarms an approved feature), so it needs
Chase's explicit per-run authorization — never do it as a silent "safety cleanup" inside a deploy.
Related: [[campaign-fix-359c1e3-preflight]], [[salesforce-connection-test]], [[tenant-and-layout]].
