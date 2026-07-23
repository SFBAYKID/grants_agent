# Rich proactive award-card campaign — reviewed design and local implementation

Status: **implemented locally, feature OFF, production needs-testing**. The design was
reviewed before code; its modules, migrations, offline tests, and CLI now exist on
`review/rich-award-card-campaign-20260723`. It extends—never replaces—the deployed
flag-off drip. Constitution (`CLAUDE.md`) and `architectural.md` govern.

Default posture: the rich campaign is **OFF**. `--dry-run` writes nothing; `rich-shadow`
is read-only; `rich-prepare` performs no HTTP/write unless `--execute` is explicit.
Enabled delivery remains separately authorized.

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

Implemented versions **14–23**: completed-run freshness; widened rich post kind and
nullable snapshot links; immutable snapshot/action/contact tables; exact Salesforce
owner/completed-call evidence; reviewed-kind and paid-attempt state; contact hash; and
the v23 exact-event truth companion plus queued-outreach action link. Historical
migrations remain unchanged.

Each preparation snapshot freezes one exact evidence version. Its v23 truth row carries
the source-qualified stable award key, event type and amount, verification/evidence
hash/locator, and official-site evidence URL. Changed contact or CRM evidence creates a
new immutable preparation row. The notification outbox—not snapshot uniqueness—uses the
stable award/audience key to prevent duplicate delivery across evidence versions.
Thread replies, actions, outcomes, and Persequor requests resolve through `snapshot_id`.

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

Freshness (named constants): `OBSERVATION_FRESH_DAYS = 6` calendar days (weekend plus
one holiday), and the observation must bind to a **completed successful run**, not
`last_seen`. Contact freshness `CONTACT_FRESH_DAYS` (proposed 30). CRM freshness reuses
the existing 24h `checked_at` window; activity `ACTIVITY_FRESH_DAYS = 30`.

Organization: v1 currently qualifies only a school district with an exact runtime NCES
identifier. The broader evidence table is forward schema, not a claimed writer path;
cities and unlinked schools remain deferred. **Name heuristics never qualify.** State
provenance uses the existing verified-source gate.

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

`GRANT_RICH_CARD_ENABLED` defaults **OFF**. Implemented CLI: `rich-prepare` is a
no-HTTP/no-write preview unless `--execute` is explicit; `rich-shadow` is deterministic,
PII-free, and read-only; ordinary `drip --dry-run` previews the selected path without a
write or external action. Enabled delivery is not authorized. No cron changes.

---

## 12. Test plan (happy + failure)

Eligibility (valid platinum/Gold NCES district; city/RFP/bulletin rejected;
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

## 13. Resolved design questions

1. Preparation snapshots are versioned by exact render/evidence content. The
   notification outbox owns the separate source-qualified award/audience delivery key.
2. `contact_evidence` is append-only and isolated from legacy `contacts` reads.
3. Preparation remains an explicit CLI; this task adds no cron.
4. Nullable snapshot links and legacy-visible `not_relevant` state preserve rollback.
5. Pacing tests cover Pacific/Eastern DST, the deterministic band, and hard cutoff.

---

## 14. Critic resolutions (v2) — supersede the sections above where they conflict

architectural-critic reviewed v1 (`36200b8`) and returned 5 CRITICAL + 6 HIGH findings,
all grounded in code and this repo's incident history. Resolutions:

**C1 — freshness has no schema under it (`source_observations` has no `run_id`; writes
are once-only so `observed_at` is first-sighting, not re-confirmation).** RESOLUTION:
add forward migration **v14a** stamping mutable re-confirmation on the LEAD projection
(NOT the immutable observation): `leads.last_confirmed_run_id` (FK-less int) and
`leads.last_confirmed_at`, updated on EVERY poll that sees the item (including the
`INSERT OR IGNORE` no-op path in `upsert_lead`). `runs` gets a stable `id` if it lacks
one. `OBSERVATION_FRESH_DAYS` is measured against `last_confirmed_at` from a run whose
`RunStats.complete` is true — never `last_seen`, never `observed_at`. This is a pipeline
change (`upsert_lead` + `cmd_poll`), design-level here, and its eligibility predicate is
UNSATISFIABLE until it lands — so the rich card cannot be built to reference "completed
run" freshness before this migration exists.

**C2 — dedup on the `event_id` surrogate re-invites the `rfp_item_id` drift incident,
and `policy_version` in the uniqueness key re-posts the backlog on any policy bump.**
RESOLUTION: delivery dedup is **policy-independent** and keyed on a STABLE identity —
`canonical_entity_key(entity,state)` + program + source namespace + the exact
USASpending record identity + `audience`. `policy_version` stays on the
snapshot as provenance ONLY, never in the uniqueness key. ADDITIONALLY retain the legacy
guard the plain drip already relies on: exclude any lead already in `posts`/
`notification_outbox` for the audience (that is what actually held the line after
`15263d2`). A content hash is explicitly rejected (a re-keyed lead has identical content
but must still dedup; field reordering changes the hash).

**C3 — the rich card MUST write a `posts` row (thread attribution goes through
`find_post_by_ts`), but `posts.kind` CHECK admits only platinum/nugget/rfp/bulletin
after v13 → a CHECK violation fires AFTER the Slack post lands = the migration-13
wedge.** RESOLUTION: migration **v15** rebuilds `posts` with the widened CHECK adding
`'rich_award'`, using the exact CREATE-copy-DROP-rename recipe as `_migration_13`,
preserving `id` for `engagement.post_id`. The rich card records `kind='rich_award'`.

**C4 — the snapshot is truth for BUTTONS but not for the THREAD REPLY (the main
engagement surface). `_handle_drip_thread` answers from `db.get_lead` (mutable) + a live
`lead_id`-keyed CRM lookup, so "who do I talk to about that award?" can contradict the
frozen card after a re-grade/re-key/CRM change.** RESOLUTION: §1 is corrected — this is
NOT a thin change. `_handle_drip_thread` is modified: when `post["snapshot_id"]` is set,
load the frozen snapshot and answer from its frozen entity/award/contact/SF fields,
labelled "as of when this card was prepared," rather than re-querying. The frozen SF
context overrides the live `lead_id`-keyed lookup for a snapshot-backed thread.

**C5 — `Not relevant` suppression must be written where the LEGACY candidate queries and
a ROLLED-BACK `264b0e2` look, or the plain drip re-posts a rejected lead.** RESOLUTION:
`Not relevant` writes the `rich_card_actions` row AND sets
`leads.status='not_relevant'` with a note (legacy selectors all
already exclude via `status='new'`). Rollback test asserts all three legacy queries
exclude a not-relevant lead.

**H1 — city `entity_kind` has no non-heuristic runtime provenance (usaspending recipient
name is a bare string; `entity_type` is frequently blank; the Census place universe is a
research queue, not linked to runtime leads).** RESOLUTION: **scope v1 to NCES-linked
school districts** through `leads.nces_id`. City and other-kind evidence rows remain
ineligible until a separately reviewed runtime writer exists.

**H2 — routing must resolve owners by exact `User.Email`/`Id`, not `Owner.Name` (the only
field the reader returns today), AND must not tag a rep who is not in the configured
channel.** RESOLUTION: `salesforce_activity.py` fetches `OwnerId` + `Owner.Email`;
routing matches `Owner.Email` == exactly one `reps.json` email → Slack id, AND verifies
that Slack id is a member of the audience channel (reuse the membership check pattern);
else fall to territory/unassigned. `Owner.Name` is never used for identity.

**H3 — removing `build_brief`'s `school_district` default hits the LIVE legacy outreach
path (`_request_outreach`), an external pinned-contract change.** RESOLUTION: do NOT
change the legacy wire shape blind. The RICH path passes a real `entity_type` mapped
from the snapshot's evidenced `entity_kind` (always populated). The LEGACY path is left
unchanged in this task; removing its fabricated default is deferred until Persequor is
confirmed to tolerate a blank `entity_type` (needs separate confirmation — I cannot
contact Persequor under this task). §9 corrected accordingly.

**H4 — the `in_flight`-before-paid-HTTP discipline must live at the FINDER boundary, not
just the worker; the legacy thread-draft path pays inside a Slack handler.** RESOLUTION:
`in_flight` markers wrap the caller-facing contact operation before its first
Firecrawl/LinkedIn/Anthropic call in both rich preparation and the legacy Slack contact
tool. A restart never blind-retries an indeterminate operation. The rich draft handler's
"re-check expiry with NO live enrichment" is a HARD guarantee: it reads frozen snapshot +
`contact_evidence` only and can never call `find_contact`.

**H5 — a snapshot FK to `funding_events(id)` would wedge the deletion-based data-
reconciliation procedure (the 2026-07-21 dup fix deleted event rows).** RESOLUTION:
snapshots store `event_id`/`observation_id`/`run_id` as PLAIN integers WITHOUT an
enforced FK (the snapshot already copies the facts), so evidence reconciliation is never
blocked by a frozen card.

**H6 — delivery-time re-validation vs immutability is undefined.** RESOLUTION: at POST
time the mutable lead is read ONLY as a veto — if the lead is `dead` or the contact
expired since prep, the card is NOT posted (re-prepare instead); the frozen snapshot is
never edited, only gated. Mutable state can veto, never supply new card content.

**M-series:** `OBSERVATION_FRESH_DAYS` reconciled to business-day cadence (proposed
6 calendar days to cover a weekend+holiday); enable-time seam (M2) specified so the rich
card REPLACES the plain nugget in `pick()` when the flag is on (never both → no double
card); per-audience "one card/weekday" stated as PER-AUDIENCE with the shared cap; hard
cutoff uses the same PT-local comparison and clamp as `slot_band()`.

**THE GATING QUESTION I CANNOT ANSWER (critic Q2).** Whether production has ANY leads
that are verified `award_announced|award_obligated` + dated ≤12mo + spend-window-open +
evidenced `entity_kind` is a read-only PRODUCTION aggregate this task explicitly forbids.
The local DB has ZERO verified award events, so the feature is unexercisable against seed
data (all award-path tests use HAND-BUILT fixtures — never "verified" against seed data).
Building is authorized and proceeds against fixtures with the flag OFF; ENABLING requires
that production aggregate under separate Chase authorization + `grants-ops-guardian`,
exactly as the gold-backlog surfacing question was gated. If production mirrors local
(≈0 award events, or ≈195 same-day CA SVPP rows), the feature is a no-op or a monotonous
CA drip and the premise must be revisited before enable.

---

## 15. Chase amendments (2026-07-23) — authoritative over §14 where they overlap

**A1 — freshness advances ONLY after a durably complete, successful run.** `runs` gains
`state` (`pending`|`complete`|`failed`). `cmd_poll` INSERTs a run row up front (state
`pending`) and holds its `run_id`; while processing it records which lead ids were
confirmed (seen) under that run WITHOUT advancing freshness; ONLY after the run is
transactionally marked `complete` AND `stats.complete` is true does it stamp
`leads.last_confirmed_run_id/last_confirmed_at` for exactly those leads, in the same
transaction as the completion mark. A failed / partial / interrupted / `--dry-run` run
NEVER advances confirmation freshness. `OBSERVATION_FRESH_DAYS` reads `last_confirmed_at`
and requires the confirming run's `state='complete'`.

**A2 — `Not relevant` uses an explicit audited `not_relevant` state, NOT `dead`.**
`leads.status='not_relevant'` (evidence preserved, not declared dead) + the audience/card
suppression row (`rich_card_actions`) + a typed `outcome_events` row. Legacy candidate
queries already require `status='new'`, so both the plain drip and a rolled-back
`264b0e2` exclude it without falsely killing the evidence, which stays available for
on-demand search/inspection. Supersedes C5's `status='dead'`.

**A3 — v1 scope is evidenced schools and school districts.** `entity_kind` keeps `city`
in the schema and the policy model, but city qualification is DEFERRED: the predicate
rejects a `city` candidate with an explicit "city-kind provenance deferred" reason until
a non-heuristic runtime city-kind source exists. Schools/districts qualify via `source`
or exact `nces` provenance (`leads.nces_id`).

**A4 — the later gate is a guardian-run SHADOW REPORT using the implemented policy**, not
an ad-hoc query. It must measure every REAL readiness requirement (award, freshness from
a completed run, org-kind provenance, contact, CRM, links, routing) — never approximate
from today's incomplete schema. Built here (`campaign/report.py` + `cli rich-shadow`),
authorized-to-run separately.
