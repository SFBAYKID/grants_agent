---
name: campaign-inclusion-four-places
description: "REPRODUCED 2026-08-11: 'which orgs are on this Campaign' is encoded in FOUR places; the _includable fix converted two, so the card promises 9 and Confirm raises PermissionError and writes 0"
metadata:
  type: project
---

# The campaign-load pipeline encodes "included" in four independent places

`_includable(item, allow_org_leads)` was added to
`grant_watch/enrich/salesforce_campaign_batch.py` to make the block-gate and the
action-filter agree. It was applied to **two** of the four places that answer the
same question. The other two still carry the old expression
`completion_mode == "full" or resolution_state == 'existing_record'`:

1. `salesforce_campaign_batch.py` `_prepare_campaign_batch` — the block gate. CONVERTED.
2. `salesforce_campaign_batch.py` `_materialize_target_action` — the action filter. CONVERTED.
3. `salesforce_campaign_batch.py` `_insert_manifest`, `approved_items` — the durable
   audit record (`approved_org_count`, `approved_selection_hash`). **NOT converted.**
4. `salesforce_campaign_execution.py` `_verify_frozen_scope`, the `included` local —
   the CLICK-TIME gate. **NOT converted.**

**Reproduced by execution** (temp DB, fake gateway, 10 IL orgs: 1 exact, 8 missing,
1 ambiguous; `allow_org_leads=True, allow_resolved_only=True`):

- preview says `• Will be added now: 9`
- `crm_campaign_batch_targets.approved_org_count` = **1**, `crm_action_items` = **9**
- `confirm_action(...)` returns
  `"Salesforce was not changed: Excluded Campaign batch item is unexpectedly mapped
  to an action"`, action state `failed`, **0 CampaignMembers created**
- the SAME scenario on the pre-change commit returned
  `"1 added, 0 already present ... Explicitly excluded/skipped before approval: 9"`

So the change turned a narrow-but-working outcome into a hard dead end, one step
later than the original incident, and only in the exact combination it was written
to fix (`allow_org_leads` + `allow_resolved_only`, which is the only pair that
clears an ambiguous org AND still creates the org-only Leads).

**Why:** `completion_mode` is only `'full'` when nothing is pending, so the moment
`allow_resolved_only` is needed the mode flips to `partial_by_user` and both
unconverted places fall back to `existing_record`-only.

## The durable lesson

Every new test in `tests/test_campaign_load_composition.py` stops at
`crm_action_items` — **preparation only**. Not one of them calls `confirm_action`
or `execute_membership`. A campaign-load change is not tested until a test clicks
Confirm, because `_verify_frozen_scope` re-derives inclusion from the manifest and
is the only gate that can refuse after the human has committed.

Related: [[sibling-caller-blind-spot]] — same failure shape (invariant fixed in one
caller, the one that actually runs left alone).

## Round 2 — 85bec38: fixed, and what the fix cost

**FIXED, verified end to end.** Migration 40 stores `included` per organization,
covered by `item_hash`. Card promises 9, `approved_org_count` 9, `crm_action_items`
9, `confirm_action` → COMPLETE with **added: 9** and the 1 ambiguous org named.
`payload_json` now records `allow_resolved_only: True`. All five sites read the
stored flag.

**BUT `_manifest_item` gaining a key changed every item_hash, and that is a deploy
hazard.** Reproduced with a `git worktree` at the pre-deploy commit: prepare a batch
on 4e20eef, confirm it on 85bec38 → `PermissionError` → 0 members, nonce spent.
Worse for `reconcile_membership`, which also calls `_verify_frozen_scope`: an
`unknown` action (Salesforce ALREADY written, awaiting verification) can never be
reconciled, never expires (`_authorize_action(require_ready=False)` skips the TTL
check), and the refusal says "Nothing changed in Salesforce" — false for exactly
that population.

**Two reporting surfaces were not converted** and still read
`resolution_state != 'existing_record'`: `slack/research_tools.py:249` and
`slack/nudge_sources.py:173`. Reproduced on a fully successful 9-org load
(`included=1` on all nine, 9 added): `salesforce_campaign_status` reports
*"It could NOT add: 8 missing. Those never reached Salesforce."* A false statement
about a rep's CRM, and the first question anyone asks after a load.

**The lesson generalises:** any change to `_manifest_item` is a schema change to
every frozen approval in flight. Bump-and-drain, or version the hash.
