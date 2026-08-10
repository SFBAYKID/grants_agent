---
name: contact-fill-first-bulk-buy
description: Deploy cee19ee→17639f8 plus the first BULK ZoomInfo purchase (12 credits, 2026-08-10) — mobile_phone 0→4 and email 22→33 on production contacts; the `--campaign` flag CRASHES (no such column); and fill-leads would PATCH 8 sandbox Lead ids that do not exist in production
metadata:
  type: project
---

**Deploy LIVE 2026-08-10T07:21Z.** `cee19ee` → `17639f8672197af69ca9f77bc1df05e708040af3`.
Listener **39941 → 41003**, **0.132 s outage**. Schema stayed **39** (no migration).
Delta 12 paths = **5 deployable** (2 mods + 3 adds: `cli_ops.py`, `contact_fill.py`,
`tests/test_contact_fill.py`) + 7 `.claude/agent-memory/**`. Closure **123/123**
byte-identical, import closure 120/120, `.env` and crontab untouched, 0 tracebacks.
`cli.py` 936 lines — back under the 1000 cap.
Backup: `~/backups/deploy-17639f8-20260810T072046Z/`.

## `fill-contacts --campaign` DOES NOT WORK — it raises, it does not just miss

`cmd_fill_contacts` queries `crm_action_items WHERE campaign_name=?`. **There is no
`campaign_name` column on that table** (columns: id, action_id, lead_id,
canonical_entity_key, operation, proposed_json, state, salesforce_id,
campaign_member_id, error, verification_state, verified_at). So the documented
invocation dies with `sqlite3.OperationalError: no such column: campaign_name`.

Worse, the `else` branch is not a fallback for a *failed* match — it only runs when
`--campaign` is EMPTY, and it selects the 25 newest **gold** leads, which is a
different set entirely. An operator who "just drops the flag" silently buys contacts
for the wrong leads.

**The working way to target a campaign**, used here: read the membership from
Salesforce and map back through `salesforce_id`.
```sql
SELECT LeadId FROM CampaignMember WHERE CampaignId='701UZ00000uW9jBYAS' AND LeadId != null
```
→ 13 ids → `SELECT DISTINCT lead_id FROM crm_action_items WHERE salesforce_id IN (…)`
→ leads **231-239, 241-244**. Then call `contact_fill.fill_contacts(conn, ids, …)`
directly with the same ceiling.

## The purchase — priced free, then bought

Stage A (free searches only) priced it at **12 credits** against a **40 ceiling**;
Stage B spent exactly that.

| | before | after |
|---|---|---|
| `contacts` rows | 85 | **97** |
| non-empty `mobile_phone` | **0** | **4** |
| non-empty `email` | 22 | **33** |
| non-empty `phone` | 16 | 16 *(unchanged — `direct_phone` is not licensed on this plan)* |
| `vendor_licensed` | 2 | **14** |
| `verified` | 20 | **20** *(no new row claims verification)* |
| ledger `consumed` | 2 | **14** |

Six spend rows, each `reserved=2 billed=2 state='settled' error=None`, and
`requested_by='cli'` — the always-empty `requested_by` recorded in
[[zoominfo-first-live-spend-20260809]] is **fixed**.

**The DNC safety property held: 5 of the 12 new contacts are do-not-call, and for all
5 BOTH `phone` and `mobile_phone` are empty (5/5 withheld).** Their emails were kept,
which is the intended behaviour.

Outcome line: `considered 13, filled 6, already had a contact 6, no ZoomInfo match 1,
stopped by budget 0, credits spent 12`.

**"already had a contact 6" is not "those leads are fine."** Those 6 hold a payable
contact LOCALLY that was never pushed to Salesforce — which is why their CRM records
looked empty in [[salesforce-prod-lead-emptiness]]. The gap there is the *sync*, not
the data, and `fill-leads` is what closes it.

## fill-leads DRY RUN — and the reason NOT to execute it yet

Proposes writes for **21 leads**, not 13. The extra 8 are exactly the
`00QVC…`-prefixed Salesforce ids that **do not resolve in the production org** —
sandbox Lead ids sitting in `crm_action_items.salesforce_id` beside the production
`00QUZ…` ones. `fill-leads --execute` would therefore PATCH 8 ids that do not exist
in production. Confirms the mixed-org column finding and gives it teeth: it is not
just a read-side annoyance, it is a **write** aimed at a non-existent record.

`MobilePhone` appears in only **2** proposals (leads 239, 242) though **4** mobiles
were bought. The lead-fill picks ONE best contact per lead, and for leads 238 and 243
the chosen contact (Chief Financial Officer / Director, Information Systems) is not
the one carrying the mobile (Director of Facilities / Superintendent). **Buying a
mobile does not mean it reaches the CRM** — the contact ranking decides.

**The dry-run summary prints `filled 0`** while listing 21 leads' worth of proposed
writes, because `filled` counts real writes. Same family as the `announce --load`
and `capability-seed` traps: read the body, never the summary line.

Related: [[salesforce-prod-lead-emptiness]], [[zoominfo-first-live-spend-20260809]],
[[fill-leads-org-website-laundering]], [[deploy-76473e5-user-memory]].
