---
name: rich-award-card-design-review
description: CRITICAL findings from the pre-impl review of docs/rich_award_card_design.md (proactive rich AWARD card, snapshot-frozen, flag-off) — durable fragilities the eventual code must resolve
metadata:
  type: project
---

Reviewed `docs/rich_award_card_design.md` (branch review/rich-award-card-campaign-20260723,
design-only, feature flag OFF) on 2026-07-22. Verdict: **Rejected — Requires Rework** at the
design level; five structural CRITICALs, all resolvable in the design before code.

**Why (the load-bearing findings — verified against grant_watch.db read-only, 2026-07-22):**

1. **Freshness predicate is unimplementable on today's schema.** Design §3 wants freshness
   from "the observation bound to a completed successful run, not last_seen." But
   `source_observations` has NO run_id/run linkage column (verified: id, lead_id, source,
   source_item_id, observed_at, payload_hash, raw_json, source_url, source_locator,
   verification_status — no run link), and `upsert_lead` writes an observation ONCE (unchanged
   re-polls INSERT-OR-IGNORE no-op), so `observed_at` is FIRST-sighting, never latest
   confirmation. So the whole backlog has stale observed_at forever, and "completed successful
   run" needs a new migration + pipeline change. The honesty claim the feature rests on has no
   data under it yet.

2. **Dedup key defeats the known drift incident + policy bump re-post.** UNIQUE(event_id,
   policy_version, audience). `funding_events.id` is a surrogate; a re-keyed lead / re-observed
   award mints a NEW event id → new snapshot → duplicate card of the same real award — the exact
   rfp_item_id drift class that already fired once (15263d2). And including `policy_version` means
   bumping the policy constant re-posts every still-eligible already-seen award. Fix: delivery
   dedup must be policy-INDEPENDENT and on a STABLE identity (canonical_entity_key + program +
   award identity + audience) PLUS keep the legacy "lead already in posts/outbox for this
   audience" exclusion.

3. **posts.kind CHECK repeat.** The rich card must write a `posts` row (thread replies resolve via
   `find_post_by_ts` over `posts`). posts.kind CHECK admits only ('platinum','nugget','rfp',
   'bulletin') after migration 13. A new kind → CHECK violation AFTER the Slack post lands = the
   permanent wedge migration 13 exists to prevent. v14-17 do NOT widen it. Must reuse an allowed
   kind or add a posts-rebuild migration.

4. **Mutable-pointer leak on the primary engagement surface.** Design routes buttons/Persequor
   through snapshot_id but is SILENT on the contextual thread reply path
   (`_handle_drip_thread` → `db.get_lead(post["lead_id"])` → `conversation.respond(mutable row)`,
   plus a live SF re-lookup keyed by lead_id). The showcased "who do I contact about that award?"
   therefore answers from the mutable lead + a fresh SF query that can contradict the frozen card.

5. **not_relevant + rollback double-post.** If not_relevant lands only in the new
   rich_card_actions table, a rolled-back 264b0e2 (or the concurrently-live plain drip) won't
   honor it and can re-post. Suppression must also land where legacy candidate queries respect it
   (leads.status/dead or a notification_outbox row).

**Grounding facts (grant_watch.db, read-only):** every funding_event (403/403) is
`record_observed`/`assumed`; ZERO verified award_announced/award_obligated exist anywhere; all 150
gold leads are record_observed with funds_start/end present but occurred_on empty. So the feature
is UNEXERCISABLE against seed data and its satisfiability against production is unknown — demand a
read-only prod aggregate (award-typed + verified + dated + open-window + evidenced entity_kind)
BEFORE building, exactly as the gold-backlog surfacing question was gated.

**High-tier durable issues:** entity_kind provenance without name heuristics is unavailable for
CITIES today (Census place universe isn't runtime-linked to leads; usaspending name is a string;
NCES only covers districts) → the "platinum city" path may be unsatisfiable honestly. SF reader
returns Owner.NAME not Owner.Id/email, so routing's "exact User→roster→Slack" needs OwnerId+User
email and a channel-membership check before mentioning. Removing build_brief's `school_district`
default has blast radius on the LIVE outreach path (entity_type is "frequently blank") — map
snapshot.entity_kind→entity_type, confirm Persequor tolerates blank. Paid-call in_flight discipline
must live at the finder boundary (enrich_lead_contact's fallback pays with no marker; _request_
outreach pays inside a handler). Snapshot FK to funding_events(id) with runtime foreign_keys=ON can
WEDGE the known dup-fix delete procedure (2026-07-21 deleted funding_events) — store event_id
without an enforced FK / ON DELETE SET NULL.

Related: [[posts-kind-check-vs-drip-kinds]], [[soft-state-tags-the-wrong-rep]],
[[rfp-aggregator-and-staleness-fragilities]], [[drip-wedge-class-remaining-paths]].
