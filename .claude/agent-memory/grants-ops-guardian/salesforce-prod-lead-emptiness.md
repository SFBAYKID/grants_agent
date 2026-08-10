---
name: salesforce-prod-lead-emptiness
description: Read-only production Salesforce verification 2026-08-10 — prod reads AND writes both hit PRODUCTION (not sandbox); campaign counts 13/0/13 confirmed; 9 of 13 campaign Leads carry nothing but LastName+State+Description, MobilePhone 0/13; and crm_action_items.salesforce_id mixes two orgs plus one wrong object type
metadata:
  type: project
---

Measured 2026-08-10 from the droplet under PRODUCTION credentials, entirely read-only
(`readonly_soql` is a GET-only transport; `verify_write_scope` is a read). Nothing created,
nothing updated, no ZoomInfo credits spent.

## Production points at PRODUCTION, for reads AND writes

| | host |
|---|---|
| READ (`SALESFORCE_MY_DOMAIN_URL`) | `d41000002jiq8eam.my.salesforce.com` |
| WRITE (`_write_my_domain()`) | **same host** |
| what `readonly_soql()` actually connected to | `https://d41000002jiq8eam.my.salesforce.com` |

`SALESFORCE_WRITE_MY_DOMAIN_URL`, `SALESFORCE_WRITE_CLIENT_ID` and
`SALESFORCE_WRITE_CLIENT_SECRET` are all **ABSENT** on the droplet, so the write path falls
back to the read credentials — which is why production writes land in production.
`verify_write_scope()`: **`is_sandbox=False`**, org `…8EAM`, instance **USA598**, name
**Monarch**. `SALESFORCE_WRITE_ORG_ID` is **PRESENT and non-empty** (so prod passes the gate
that fails on a laptop without it), `SALESFORCE_WRITE_EXPECT_SANDBOX=0`,
`SALESFORCE_CAMPAIGN_WRITES_ENABLED=1`. **Campaign writes are armed against production.**

Contrast with a developer laptop, whose `.env` points writes at the **monarchdev sandbox** —
so any Salesforce result measured locally says nothing about production.
[[salesforce-connection-test]] is the recipe; this is the production answer.

## Campaign member counts (`COUNT(Id)`, never `COUNT()`)

| campaign | Id | created | members |
|---|---|---|---|
| California Grant 2026 | `701UZ00000te467YAA` | 2026-07-23 | **13** (13 Leads) |
| California Grant 2026 - Batch 2 | `701UZ00000tekqrYAA` | 2026-07-23 | **0** |
| CA Gold Aug 2026 | `701UZ00000uW9jBYAS` | 2026-08-10 | **13** (13 Leads) |

The long-reported "13 members" for California Grant 2026 is **confirmed correct**. Batch 2 is
still empty. `COUNT()` returns its total in `totalSize` with ZERO rows — always `COUNT(Id)`.

## Chase's complaint is accurate, and the number is 9 of 13

Across the 13 Leads on CA Gold Aug 2026:

| field | filled |
|---|---|
| LastName / State / Description | **13/13** (written at creation) |
| Industry | 5/13 |
| City / Number_of_Students__c | 4/13 |
| Title / Email | 3/13 |
| Phone / Street / PostalCode / Website | 2/13 |
| **MobilePhone** | **0/13** |
| FirstName / Country / NumberOfEmployees | 0/13 |

Only **4** Leads carry real contact data — Montebello (12 fields), Birmingham (10), Fairfax
(8), Valle Lindo (8) — and a 5th (San Ysidro) got only `Industry`. That reconciles with the
"27 fields across 5 Leads" `fill-leads` claim. **The other 8 hold nothing but LastName,
State and Description**, so a rep opening one sees an organisation name and a paragraph.
All 13 are `Status='New'` with a single OwnerId.

**`MobilePhone` empty on every Lead is the same story as `contacts.mobile_phone` 0/85**
([[deploy-8cb557a-watchdog-boot-revert]]): the column exists and the pipeline works — a real
paid ZoomInfo enrich returned a mobile — but the paid path has been run about twice against a
1000-credit allowance, so there is nothing to write. It is a *usage* gap, not a code gap.

## `crm_action_items.salesforce_id` MIXES TWO ORGS AND ONE WRONG OBJECT TYPE

22 distinct ids stored locally; **only 13 resolve as Leads in production**.

- **21 carry the `00Q` (Lead) prefix, 1 carries `00T` — a Task id in a Lead-id column.**
- 8 of the `00Q` ids do not resolve in production at all. `IsConverted` is 0 for all 13 that
  do, so nothing was converted away — the 8 are almost certainly **monarchdev sandbox** Lead
  ids written during sandbox testing.
- Nothing in the row records WHICH org an id came from.

So a lookup keyed on that column can silently miss, and "the Lead is gone" and "that id was
never in this org" are indistinguishable. Worth a column or a check before anything trusts it.

Related: [[salesforce-connection-test]], [[salesforce-writer-fls]],
[[campaign-writes-flag-armed-in-prod]], [[org-column-coverage-20260810]],
[[fill-leads-org-website-laundering]], [[zoominfo-first-live-spend-20260809]].
