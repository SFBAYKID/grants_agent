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

## RESOLVED 2026-07-21 (data cleanup + code guard 15263d2)

Chase ran the guarded 5-write transaction himself (the tool gate blocked the guardian
twice — see [[coordinator-stop-is-stop]]; chat approval never opens the gate). Deleted
9564 + 9534 and their `funding_events`, then re-keyed 9533 onto the full-title key.
Chase's explicit call: **"keep the observations"** — `source_observations` was left
completely untouched, so two rows (#11892 lead_id=9564, #10642 lead_id=9534) are now
deliberately ORPHANED. Consequence to remember: `PRAGMA foreign_key_check` now reports
**2 rows against source_observations forever** — that is the retained evidence trail, NOT
corruption. `integrity_check` is still `ok`. Do not "fix" it.

Verified after: rfp leads exactly [9533, 9565, 9566], all keys current full-title format,
no shared detail_url, `rfp_candidates` = [9566 due 07-22, 9565 due 07-23] with **9533
correctly absent** (posted leads are excluded by `l.id NOT IN (SELECT lead_id FROM posts)`),
post row 18 still resolves, observations still 11894, funding_events 11894→11892.

Schema facts that made the fix work (worth keeping): `source_observations` is
UNIQUE(source, source_item_id, payload_hash) — **lead_id is NOT in the key**, so a re-poll
collides with the retained orphan and reuses it; `funding_events` is UNIQUE(observation_id),
so deleting the dup's event is what frees the slot for 9533 to mint a fresh event bound to
that retained observation. Keeping the observation but ALSO keeping its event would have
blocked that.

Note the code guard alone would NOT have repaired this: `_adopt_drifted_lead` only fires
when no row holds the incoming key, and 9564 already held it. Data fix was the necessary
part; 15263d2 prevents recurrence.

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

**2026-07-22 recount over ALL 638 gold leads** (the 546 above is only the `status='new'`
subset; the rest are 75 `dead` + 17 `surfaced`): `ca-grants-award:2024-2025` 347 +
`ca-grants-award:2023-2024` 5 + `seed:svpp_csv` 75 — all three sources `occurred_on` NULL —
and `usaspending:16.071` 211, every row `occurred_on='2025-10-10'` (min==max). The
date-poverty finding is unchanged and now covers the seed rows too: **no gold source on the
droplet carries per-record award dates**, so `backfill` (a >90-day-old `dated` test in
`usaspending.parse_*`, where a MISSING date also counts as backfill) evaluates true for
every one of them.
