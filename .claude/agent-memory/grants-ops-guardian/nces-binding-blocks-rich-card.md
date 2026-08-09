---
name: nces-binding-blocks-rich-card
description: No nces_id means NO rich card, ever — and USAspending recipient names carry "NO 46"-style suffixes that defeat NCES exact-name matching
metadata:
  type: project
---

The rich award card is hard-gated on an NCES binding. `campaign/preparation._kind()` returns
`("", "")` unless `leads.nces_id` is set; `campaign/policy.evaluate` then rejects at
`entity_kind not in SUPPORTED_ENTITY_KINDS` → **`entity_kind_unsupported`**. No amount of
website/contact/CRM enrichment can rescue a lead with a null `nces_id`.

`nces.match_district` requires a **unique exact normalized-name** match and returns `None` on
ambiguity or near-miss — correct anti-fabrication behavior, but it fails on a whole class of
real leads.

**Verified 2026-08-06, lead #1603 (`usaspending:16.071`):** entity name
`HOXIE SCHOOL DISTRICT NO 46` normalizes to `hoxie no 46`. NCES AR has exactly one Hoxie
district — `HOXIE SCHOOL DISTRICT`, nces_id `0507990`, city Hoxie, enrollment 860 — which
normalizes to `hoxie`. Unequal, so no bind, so no rich card. The district number suffix comes
from the USAspending *recipient* name; NCES `LEA_NAME` has no such suffix.

**Why:** this is the likeliest reason `rich_card_snapshots` sat at 0 rows in production long
after the feature was enabled — the eligible pool is limited to leads that happen to have a
name NCES spells identically, not to leads with good award evidence.

**How to apply:** when a rich card "won't render", check `leads.nces_id` FIRST — it is the
cheapest gate and it fails before any paid enrichment matters. Before proposing a fix to
`normalize_name` (e.g. stripping a trailing `NO <digits>` / `DIST(RICT)? NO` suffix), treat it
as a lead-identity change needing Chase's approval and a false-positive audit across states:
loosening an exact matcher is exactly the kind of change that can bind a lead to the wrong
district, and a wrong `nces_id` propagates into a frozen card snapshot.

**RESOLVED for #1603 2026-08-06 by operator-named LEAID, not by loosening the matcher.**
Chase's preferred shape: the operator passes one verified LEAID, the script filters the
already-fetched `fetch_state(<lead's state>)` list by that id, and binds only on
`len(matches) == 1`, taking name/city/state/enrollment from the NCES record itself — never
hand-typed. A cross-state id resolves to 0 records and binds nothing.

**Corroborate a LEAID from TWO independent sources before binding.** For #1603: (1) NCES AR
had exactly one record with LEAID `0507990` (`HOXIE SCHOOL DISTRICT`, city Hoxie, AR, 860),
one AR district with HOXIE in the name, and 290 records / 290 distinct ids; (2) the public
USAspending award API (`/api/v2/awards/<generated_internal_id>/`) returned recipient
`HOXIE SCHOOL DISTRICT NO 46`, city HOXIE, county LAWRENCE, AR 72433. Note `leads.raw_json`
for a usaspending lead holds ONLY award id/amount/dates — no recipient address — so the
award API call is the way to get the location corroboration.

**A bound `nces_id` still does NOT produce a draft-ready card.** `policy` sets
`DRAFT_READY` only when `crm_draft_safe AND website_proven`, and
`EXACT_WEBSITE_PROVENANCE = {nces, authoritative_directory}`. A website found by scraping
yields `verified_org_page`, which caps the card at `research_needed` (no Persequor draft
button, "Not relevant" only) even with a clean `complete_no_match` CRM. Related:
[[oneoff-scripts-need-load-dotenv]], [[rich-card-enable-20260805]].
