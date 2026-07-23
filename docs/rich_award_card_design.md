# Rich proactive award-card campaign — design (pre-implementation)

Status: **design, needs-testing** (nothing built or enabled). This is the design the
architectural-critic reviews before any code, per the campaign spec (2026-07-23). It
extends — never replaces — the deployed drip. Base branch: `review/rich-award-card-
campaign-20260723` from `99c0240`. Constitution (`CLAUDE.md`) and `architectural.md`
govern; where this doc and those disagree, they win.

Default posture: the rich campaign is **OFF**. `--dry-run` writes nothing; shadow mode
prepares local state only; enabled delivery is NOT authorized by this task.

---

## 0. Guiding invariants (preserve, never weaken)

Immutable observations/events; required `RawItem.event_type`; event-type record
semantics (`record_semantics.py`); reservation-before-Slack (`db_delivery.py`);
fail-closed ambiguous send; channel guards + operator visibility (`cli drip-blocked` /
`drip-unblock`); exact roster + verified Slack IDs; URL hardening + unfurls off; typed
Salesforce lookup states; verbatim contact verification; durable idempotent Persequor
intake; Slack-receipt + Salesforce-action replay protection. Salesforce ownership never
defaults to the integration user, requester, territory rep, or anyone.

---

## 1. Module layout (respect the 1000-line cap; split before ~800)

`drip.py` and `grant.py` are already large; rich-card logic goes in NEW focused modules:

```
grant_watch/campaign/
  __init__.py
  policy.py        # eligibility predicate + freshness constants (pure, testable)
  snapshot.py      # typed immutable card-snapshot model + freeze/load
  routing.py       # routing precedence (call-owner > acct/opp > territory > unassigned)
  preparation.py   # bounded preparation worker + in_flight paid-call discipline
  card.py          # Block Kit builder + accessible fallback text (pure render)
  report.py        # PII-free preparation/shadow report
grant_watch/enrich/salesforce_activity.py   # read-only completed-call evidence (extends reader)
```

`slack/grant.py` gains only two thin action handlers (`rich_draft`, `rich_not_relevant`)
that delegate into `campaign/` — no policy logic in the Bolt layer. `persequor_client.py`
loses the fabricated `school_district` fallback and gains snapshot-bound intake.

---

## 2. Migrations (forward-only, after v13; never reuse 10–12; never edit history)

Next versions **14–17**. Each is additive (new tables / nullable columns), so old code
(`264b0e2`/`99c0240`) that never references them keeps working → rollback-safe.

- **v14 `rich_card_snapshots`** — the immutable frozen card. One row per prepared card.
  Columns (all frozen at creation): `id` (uuid), `policy_version` (int), `lead_id`,
  `event_id` (exact funding_events.id), `observation_id`, `run_id`, `tier`
  (`gold`|`platinum`), `entity_name`, `entity_kind` (`city`|`school`|`school_district`),
  `entity_kind_provenance` (`source`|`nces`|`census`|`reviewed`), `state`,
  `state_provenance`, `program`, `amount`, `award_date`, `award_date_precision`,
  `spend_window_start`, `spend_window_end`, `award_url`, `official_website`,
  `contact_name`, `contact_type` (`named_direct`|`official_general`), `contact_email`,
  `contact_evidence_url`, `contact_verified_at`, `contact_expires_at`,
  `sf_lookup_status`, `sf_account_id`, `sf_open_opp_id`, `sf_activity_id`,
  `sf_display_text`, `sf_open_link`, `routing_reason`
  (`sf_call_owner`|`sf_account_owner`|`sf_opp_owner`|`territory`|`unassigned`),
  `slack_user_id` (nullable → unassigned), `fallback_text`, `render_inputs_json`,
  `created_at`, `expires_at`, `state_updated_at`. **No mutable pointer** — the snapshot
  is the truth; nothing reads `leads.current_event_id` after freeze.
  UNIQUE `(event_id, policy_version, audience)` prevents duplicate delivery of the same
  event/policy/audience. `audience` stored alongside for the constraint.
- **v15 `rich_card_actions`** — action state keyed by snapshot: `snapshot_id`,
  `action` (`draft`|`not_relevant`), `nonce`, `requester_slack`, `state`
  (`requested`|`accepted`|`rejected`|`blocked_expired`), `created_at`, `updated_at`,
  UNIQUE `(snapshot_id, action, nonce)` and a partial UNIQUE `(snapshot_id, action)` for
  `draft` so a double-click/replay collapses to one request.
- **v16 `contact_evidence`** — forward-only contact lifecycle (does NOT edit `contacts`):
  `id`, `lead_id`, `status` (`verified`|`superseded`|`removed`|`unavailable`|
  `not_found`), `contact_type`, `name`, `title`, `email`, `official_evidence_url`,
  `official_domain`, `evidence_hash`, `first_verified_at`, `last_checked_at`,
  `last_verified_at`, `expires_at`. Append-only: a re-verify inserts a new row and marks
  the prior `superseded`; the current contact is the latest non-superseded row.
- **v17 link columns**: add nullable `snapshot_id` to `posts` and to
  `notification_outbox` (both default NULL). Old code ignores them; new code resolves
  every reply/action/outcome/Persequor request through `snapshot_id`.

Rollback: old code never selects these tables/columns; `apply_migrations` never
downgrades; a higher `schema_migrations` MAX is inert to `264b0e2`. Documented and
tested (fresh DB + every supported historical schema + rollback compatibility).

---

## 3. Eligibility policy (`campaign/policy.py`, pure)

A candidate qualifies only if EVERY rule holds (else the card is ineligible; if no
candidate qualifies, **post nothing** — never fall back to RFP/bulletin/stale/generic):

Award: stored grade GOLD; verified `award_announced`|`award_obligated`; positive finite
amount; award date exact enough for the wording, not future, `≤ 12 months`; spend window
explicit and currently open. Platinum is a presentation tier only (verified award `≤ 7`
days meeting the existing strong physical-security program rule).

Freshness (named constants): `OBSERVATION_FRESH_DAYS = 4` calendar days (covers
weekends), and the observation must bind to a **completed successful run**, not
`last_seen`. Contact freshness `CONTACT_FRESH_DAYS` (proposed 30). CRM freshness reuses
the existing 24h `checked_at` window; activity `ACTIVITY_FRESH_DAYS = 30`.

Organization: explicitly evidenced `city`|`school`|`school_district`, with stored
provenance = source | exact NCES/Census | separately reviewed. **Name heuristics alone
never qualify.** State provenance verified (the existing `VERIFIED_STATE_SOURCES` gate).

Links/contact/CRM: exact public award-record URL present + URL-safe; official website
evidenced; a fresh public work contact exists (named-direct or official-general, never a
personal-provider mailbox, email verbatim on the official evidence page at last verify);
Salesforce lookup fresh AND complete — safe states are `exact_match` |
`complete_no_match` | another explicitly reviewed complete state. Ambiguous/partial/
unavailable/stale CRM ⇒ temporarily ineligible.

Never label an obligated amount "remaining"/"available"/"left to spend".

---

## 4. Routing precedence (`campaign/routing.py`)

1. Approved-roster owner of a **recent verified completed Salesforce call** (§5).
2. Approved-roster owner of an **exact Salesforce Account or open Opportunity**.
3. **Verified territory** owner (`territory.py`, the deployed map).
4. **`Unassigned territory`** — NO Slack mention.

Relationship/CRM ownership beats territory. Every owner resolves via exact Salesforce
User ID/email → exactly one approved-roster identity → Slack ID; **no fuzzy name match**,
never a guessed/default owner. The chosen Slack user, `routing_reason`, supporting SF
IDs, and unassigned flag are frozen in the snapshot.

---

## 5. Salesforce activity evidence (`enrich/salesforce_activity.py`, READ-ONLY)

Extends the read-only boundary only. A card may say a rep called someone only when SF
proves ALL of: exact org/account match; correct Account↔Contact/Lead relationship;
completed Task; `TaskSubtype='Call'` (or an explicitly reviewed call type); verified
completion timestamp; activity `≤ 30` calendar days; exact Task owner SF User ID; exact
User→roster→Slack mapping. Never infer a call from `Subject`/`LastActivityDate`/generic
activity. Persist a typed `sf_activity` snapshot (record id+type, completion ts,
Account+Contact/Lead ids, owner User id + safe display, record link, lookup status +
checked ts, evidence/error state). Do NOT import/invoke the Campaign writer.

---

## 6. Preparation worker (`campaign/preparation.py`, bounded)

Refreshes the top-N candidates' contact + CRM + activity evidence BEFORE the delivery
window, so Slack delivery and button handlers do **no** slow/paid work. State machine
per candidate: `pending → preparing → ready | ineligible | error`. Any possibly-paid
call (Firecrawl/LinkedIn) writes an `in_flight` row **before** the HTTP request; a
restart finding `in_flight` does NOT silently retry the indeterminate paid request — it
requires an explicit `--retry-indeterminate`, mirroring `source_discovery_models`. Dry-
run: no network/db. Shadow: prepares local evidence/state, no Slack/Persequor/SF writes.

---

## 7. Pacing & diversity (extend deployed drip; do not weaken)

One proactive-message cap per weekday (reuse `DAILY_CAP=1`). **Remove the urgent/
exceptional second-post path** for the rich campaign. Keep deterministic per-day
slotting; default band ~10:00–11:00 PT; add a named configurable hard cutoff
`RICH_HARD_CUTOFF_PT` (recommended 11:30) after which a missed card waits a day.
Follow-up reminders stay default-off and share the cap. State diversity across posting
days is already deployed (`_best_nugget` cooldown via `db.recent_post_states`); the rich
selector reuses it: prefer a state ≠ most recent, deterministic fallback for one
remaining state, score order preserved within a state, deterministic (state, lead-id)
tiebreak, no random variety. Write-free dry-run preserved.

---

## 8. Block Kit card (`campaign/card.py`, pure render + fallback text)

Blocks: GOLD/PLATINUM heading; rep mention or `Unassigned territory`; entity+state;
amount+program+accurately-labeled award date; clearly-labeled spend window; SF context
only when its typed snapshot supports it; named work contact or clearly-labeled official
general mailbox; accurately-labeled links (Official website / Contact evidence / Open
Salesforce only on exact safe match / View exact award record); actions `Ask Persequor
to draft`, `Not relevant`. Sanitize every source field (`plain_fragment`); enforce Block
Kit + fallback length caps; unfurls off; action `value` = opaque snapshot-id + nonce
only (no email/URL/CRM/PII). Never "Anybody want to talk?"; never "View the source
record" for an award; never imply eligibility proves a purchase.

---

## 9. Persequor action (`persequor_client.py` + handler)

Remove the fabricated `school_district` default in `build_brief` — unknown org type
stays unknown and cannot qualify a rich card. On `Ask Persequor to draft`: verify
workspace/channel/thread/requester/roster; bind to the exact snapshot + actual
requester; idempotent on double-click/Slack retry (UNIQUE `(snapshot_id, action)`); use
the existing durable outbox; submit only frozen snapshot facts; re-check snapshot +
contact expiry with NO live enrichment; if expired, block and request re-preparation;
report only "Persequor accepted a draft request"; never claim an email was sent. Any
approved rostered rep in the configured channel may request it.

---

## 10. Feedback & report (`campaign/report.py`)

Typed, deduplicated outcomes: card delivered; thread reply/question; draft requested;
Persequor accepted/rejected/unavailable; not-relevant/bad-lead; safely-observed SF
action. `Not relevant` records feedback and suppresses repeat proactive delivery of that
snapshot's event WITHOUT deleting evidence. Do NOT claim hyperlink clicks are
measurable. PII-free shadow/preparation report: candidate count; ready count; rejection
counts by policy reason; contact/CRM/org-kind/source-run-link readiness; mapped vs
unassigned routing counts.

---

## 11. Feature flag, dry-run, shadow (`campaign/__init__.py` + `cli.py`)

`GRANT_RICH_CARD_ENABLED` defaults **OFF**. CLI (documented in `--help`): `cli
rich-prepare [--dry-run|--shadow]`, `cli rich-review` (deterministic, PII-free),
`cli rich-drip --dry-run` (write-free preview). Boundaries: **dry-run** = no db/Slack/
SF-write/paid-call/Persequor; **shadow** = local preparation state allowed, no Slack/
Persequor/SF writes; **enabled delivery** = not authorized this task. No cron changes.

---

## 12. Test plan (happy + failure)

Eligibility (valid platinum city; valid gold school/district; RFP/bulletin rejected;
nonprofit/unknown/heuristic-only kind rejected; award-date boundaries; closed/missing
window; stale observation; incomplete run; unsafe/generic URL; stale/incomplete
CRM/contact; no candidate ⇒ silence). Diversity/pacing (no consecutive same-state;
one-state fallback; within-state order; weekday slot; PT/ET + DST; one-message cap; no
urgent second post; hard cutoff; dry-run writes nothing). Salesforce (exact account +
completed call; generic activity ≠ call; open Task rejected; wrong binding rejected;
ambiguity/partial/unavailable; call-owner > acct/opp > territory; unassigned when none
resolves). Contacts (fresh named; official general; stale/removed/superseded/unreachable;
personal-provider rejected; domain mismatch; paid in-flight/restart). Snapshots/actions
(snapshot unchanged after mutable lead/event/contact/CRM updates; thread+Persequor use
snapshot; reservation precedes Slack; ambiguous send never blind-retries; renderer
failure doesn't wedge; action replay/double-click ⇒ one request; wrong requester/
workspace/channel/thread rejected; expired evidence blocks drafting; no PII in
values/logs). Presentation (gold+platinum Block Kit; fallback text; injection + URL
hardening; length limits; accurate links; no remaining-money/guaranteed-purchase/
fabricated-owner/email-sent claims). Migrations (fresh db; every supported historical
schema; preserve posts/outbox/contacts/CRM/engagement; FKs + uniqueness; rollback
compat; no historical edits).

---

## 13. Open design questions for the critic

1. Snapshot `audience` in the UNIQUE key vs a separate posts link — is the constraint on
   `(event_id, policy_version, audience)` sufficient, or should it be
   `(snapshot content hash)` to prevent a re-prepared snapshot double-posting?
2. `contact_evidence` append-only vs mutating `contacts` — confirm the forward-only table
   is the right isolation and that the existing `contacts`-based flows are unaffected.
3. Preparation worker cadence: a new cron vs folding into the existing poll — the spec
   forbids production cron changes, so this must run only under explicit CLI in this task.
4. Rollback: after enable, a `not_relevant`/snapshot row exists; confirm `264b0e2`/
   `99c0240` selection ignores it (they don't read snapshots) — but the v17 `posts.
   snapshot_id` column must have a default so old INSERTs still work.
5. DST + hard cutoff interaction with the deployed slot-band clamp (04:00–16:30 PT).
