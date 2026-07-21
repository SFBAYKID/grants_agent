---
name: rfp-dedup-key-drift
description: RFP duplicate leads come from a dedup-KEY FORMAT migration (6-token prefix to full title), not case-sensitivity — plus the gold backlog has no usable award dates
metadata:
  type: project
---

Verified read-only on the droplet 2026-07-20 (deployed revision f4d6237).

## RFP duplicates = dedup-key format migration, NOT a parser bug

`rfp_parse.rfp_item_id()` builds `{entity}|{title_tokens}|{due_iso}` and already lowercases,
so **pure case differences can never produce a duplicate**. The real cause:

- Commit **d317e6f** (2026-07-18) keyed RFPs on a **6-token title prefix**.
- Commit **eabf6e5** (2026-07-19, "honesty + correctness bugs from RFP/export bug sweep")
  switched to the **FULL normalized title** — deliberately, because two bid packages of one
  project ("…General and HVAC Construction" vs "…Plumbing Construction *REBID*") share the
  first six tokens and the prefix key silently collapsed them, dropping a real solicitation.
- Leads ingested BEFORE that deploy kept their short legacy key. On the next poll the new
  parser computed a different key for the SAME solicitation, so `upsert_lead` inserted a
  second row.

Proof method that settles it (reusable): recompute `rfp_item_id()` from each lead's OWN
stored `entity_name/title/funds_end/detail_url` and compare to the stored `source_item_id`.
Mismatch == legacy key == will re-duplicate on the next poll. Corroborate with
`GROUP BY detail_url HAVING COUNT(*)>1` — the true-duplicate signal.

2026-07-20 blast radius: only **5 `source='rfp'` leads exist**; 2 carried legacy keys
(#9533 PA DOC, #9534 CA DGS) and **both had already re-duplicated** (#9533→#9564,
#9566→#9534, each pair sharing one `detail_url`). #9565 is NOT a duplicate — it is the
genuinely distinct "Plumbing Construction \*REBID\*" package with its own URL. So: 2 real
dup pairs, 1 legitimate sibling. A fix is a one-time key reconciliation of legacy rows, not
a parser change.

**Orphan lead #9534** (found chasing why it was missing from `rfp_candidates`): it is
`lead_grade='gold'` + `event_type='rfp_posted'`, which matches NEITHER drip pool —
`rfp_candidates` requires `lead_grade='silver'`, `nugget_candidates` requires an
`award_*` event type. It can never be posted. Cause: commit f7dfddc ("RFPs Silver not
Gold", deployed 2026-07-19) landed between the two ingests, so the 07-19 row kept the old
gold grade while its 07-20 twin (#9566) is silver. Grade drift and key drift travel
together — check BOTH when reconciling legacy rows.

**Watch for:** the stored title of #9565 contains literal backslashes (`\*REBID\*`) —
escaped Slack markdown persisted into the DB, which also feeds the dedup key.

## The gold backlog has NO usable award dates (blocks any freshness cutoff)

For the 546 suppressed backfilled `award_obligated` gold leads:
- `ca-grants-award:*` (351 rows, 64%): `e.occurred_on` is **NULL/empty**, and `raw_json`
  holds no award date at all — only `PublishDate`/`LastUpdated` (portal metadata) and
  `FiscalYear` ("2024-2025").
- `usaspending:16.071` (195 rows, 36%): `e.occurred_on` = **2025-10-10 for all 195**
  (min==median==max). `raw_json` "Base Obligation Date" has **distinct=1**, as do
  "Start Date" (2025-10-01) and "End Date" (2028-09-30). Only `Award Amount`/`Award ID` vary.
- `l.funds_start` has only **3 distinct values** across all 546.

So a `<12mo` freshness line on `occurred_on` is all-or-nothing: it admits exactly the 195
usaspending rows and excludes all 351 CA rows for absence of data, not for staleness.
`assumed`: the single 2025-10-10 is a batch import artifact rather than 195 awards truly
obligated the same day. Do not present these as per-record award dates to Chase.

Composition: 545 distinct `entity_name` / 546 distinct `canonical_entity_key` for 546 leads
— essentially one award per org, not a few orgs repeated. Heavy CA skew (CA n=356 = 65%).
See [[drip-pacing-and-cap]] for the read-only probe recipe.
