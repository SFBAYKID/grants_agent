---
name: five-design-review-2026-08-09
description: Pre-implementation review of fetch_url / honesty fixes / campaign chunking / ZoomInfo / proactive follow-ups — verdicts, the load-bearing evidence, and the M1 confirmation
metadata:
  type: project
---

Chase's five authorized changes after the Nelly incident (347 CA silver leads never reached a
Campaign). Reviewed read-only 2026-08-09 against running code.

**Verdicts:** D1 fetch_url REJECTED as specified (see [[tool-result-crm-marker-injection]]).
D2 honesty fixes APPROVED except part (b). D3 chunking APPROVED WITH REQUIRED CHANGES
(migration must not rebuild the table). D4 ZoomInfo APPROVED-IN-PRINCIPLE, blocked on a
provenance model + a compliance answer. D5 follow-ups BLOCKED on the M1 pacing fix.

Durable facts worth not re-deriving:

- **M1 is still live and one-way.** `drip.pacing_ok` (drip.py:345-347) counts only
  `posts_today` + `delivery_attempts_today`. `salesforce_followups._used_slots`
  (salesforce_followups.py:203-221) counts drip's posts AND its own rows — so followups
  defers to drip, drip never defers to followups. `pacing.reserve_daily_slot`
  (campaign/pacing.py:38-60) IS atomic and IS read by `campaign/pacing.should_post`, but the
  rich→daily FALLBACK path runs legacy `run_drip`, which reads neither. Invariant the fix must
  establish: every proactive sender claims the day through ONE primitive before its first
  Slack call, and every cap check reads that same primitive.
- **Org-phone-as-person's-phone already ships.** `salesforce_contact_records.py:260-266`
  writes `payload["Phone"] = org_phone` on a PERSON Lead when the person has none, and the
  note body repeats it (`:360-362`). Email has a typed `email_kind` with an explicit
  "org general — not the individual's" label (`:521`); phone has no equivalent. Design 2(b)
  would spread this defect, not create it.
- **Phone was never deliberately excluded from search output.** `git log -S'_CONTACT_COLUMNS'`
  returns exactly one commit (604069d, the feature's introduction); `git log -S'contact_phone'`
  returns nothing. It is an omission. `docs/question_bank.md:72` lists asking for a phone as a
  supported question.
- **`contact_suffix` silently truncates.** search_presentation.py:112 unpacks
  `(list(cell)+["","","",""])[:4]` — a 5th cell is dropped from the Slack summary with no
  error, and inserting phone before `status` binds status to the phone string.
- **`ux_crm_one_ready_campaign_creation` does NOT block multi-slice member actions** — the
  partial index (migrations_campaign_preview.py:41-42) is scoped to
  `action_type='create_campaign'`; members use `add_campaign_members`. Nothing bounds how many
  ready member actions accumulate per thread (TTL 24h, salesforce_campaigns.py:52).
- **`UNIQUE(batch_id, campaign_id)` is an INLINE table constraint**
  (migrations_campaign_batch.py:88) with live FK children (`crm_campaign_batch_items.target_id`
  :92, `crm_actions.batch_target_id` :37). Changing it means a SQLite table rebuild whose
  rollback is restore-from-backup. Sibling batches linked by a nullable `parent_batch_id`
  avoid it entirely.
- **The >200 raise (salesforce_campaign_batch.py:525) is not the only path that persists
  nothing** — everything before `_insert_manifest` (:642) is invisible, including
  `verify_write_scope`, `parse_record_link`, `get_record`, and the expensive
  `resolve_organizations`.
- **ZoomInfo's laundering path is one string literal.** `db.save_contact` (db.py:619) takes
  `contact_status` as a parameter; `== 'verified'` comparisons that would then accept it live
  at grant.py:611, grant.py:621, contact_enrichment.py:59, salesforce_contact_records.py:444.
  Separately, `policy.contact_binding` (campaign/policy.py:355-373) binds on email DOMAIN
  alone — a ZoomInfo email at the district's own domain passes `ContactBinding.ORG_SITE`
  without ever having been seen on a page.

**How to apply:** the correct build order is D2(c)(d)(a) → D3's failure-record half → the M1
pacing fix → D3 chunking → D1 → D5 → D4. Highest probability of shipping a bug is D3 (only one
mutating a live schema); highest blast radius is D1.

**The cheaper fix nobody proposed:** `salesforce_campaign_members_preview` already accepts a
`search_request_id` snapshot (tools.py:646-662) and `search_leads` already persists one with
`result_complete` (search.py:807). Wiring that same input into the BATCH tool gives every
`search_leads` filter to campaign selection, makes "refine the request" a true statement, and
needs no migration and no slicing.
