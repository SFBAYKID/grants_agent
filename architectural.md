# architectural.md — grants_agent

Companion to `CLAUDE.md`. This is the system design. Keep it under 1000 lines; split if it grows.
Every design decision here serves the Constitution in `CLAUDE.md` — especially "never fabricate data"
and "tenant isolation is sacred."

---

## 1. What the system is

A scheduled pipeline that discovers fresh government **security-funding leads** (schools/cities that
just received or are applying for physical-security money), enriches them with a **public** point of
contact, and surfaces them to a human through **Grant**, a Slack chatbot, with a human-approved
outreach handoff to **@Persequor**.

Data flow:

```
  gov APIs / PDFs / bid portals
            │  (pollers, one module per source)
            ▼
     normalize → score (GOLD/SILVER/watch) → dedup on (source, source_item_id)
            │
            ▼
 immutable observations/events ──► lead projection ──► contact + NCES + CRM snapshots
            │                                  │  never fabricate — unknown is valid
            ▼                                  ▼
 scheduled workers ──► Grant (Slack/search/export) ──► approved Persequor/Campaign actions
```

Local source, enrichment, Slack, search/export, read-only CRM, and create-only Campaign workflows are
implemented **and running in production**. The droplet runs a crontab of scheduled workers (polling,
the daily card, follow-ups at `*/15 8-14 * * 1-5`, reminders, thread scanning, the watchdog); the
Socket Mode listener is long-lived. Deploys are hash-pinned from `origin/main` by the
grants-ops-guardian — see §6.

---

## 2. Repository layout

**Current package:**

```
grants_agent/
├── AGENTS.md                 # tool-neutral agent workflow and health gate
├── CLAUDE.md                 # constitution + mission
├── architectural.md          # this file
├── .env / .env.example       # secrets (real .env git-ignored)
├── requirements.txt
├── grant_watch/              # typed application package
│   ├── migrations.py         # ordered SQLite migrations and durable workflow state
│   ├── source_catalog.py      # discovery evidence validation + generated access reports
│   ├── source_discovery.py    # immutable Firecrawl selected-result evidence
│   ├── coverage_universe.py   # Census county universe + sharded research tasks
│   ├── sources/              # one official source per module
│   ├── enrich/               # contacts, NCES, ZoomInfo, Salesforce reader + Campaign gateway
│   ├── campaign/             # the rich award card: policy, snapshot, routing, delivery
│   ├── notify/               # Resend email transport (to reviewed reps only)
│   └── slack/                # proactive alerts, conversation tools, follow-ups
├── data/source_catalog/       # canonical nationwide source candidates + gap evidence
├── docs/source_inventory/     # generated public/keyed/access/coverage catalog views
├── data/svpp_active_awards_CA_MI_PA_WA.csv   # 75 verified GOLD seed leads
├── docs/FINDINGS.md
├── docs/grant_lead_source_inventory.md
├── docs/grant_agent.md       # Grant (Slack bot) spec + live app config record
├── assets/                   # Grant logo (owl) — also set as the Slack app icon
└── .claude/agents/           # project-scoped agents (grants-ops-guardian, architectural-critic)
```

**Responsibility split (each file remains below the 1000-line cap):**

```
grant_watch/
├── __init__.py
├── models.py           # typed source, funding-event, lead, and run dataclasses
├── db.py               # SQLite repository operations; schema lives in migrations.py
├── migrations*.py      # 39 ordered migrations across several modules; NEVER mutate one in place
├── source_catalog.py   # typed candidate catalog, evidence validation, generated reports
├── source_discovery.py # immutable Firecrawl search and scrape fingerprints
├── coverage_universe.py # pinned Census county universe and per-entity research status
├── health.py           # docs/annotations/line-cap/nested-test-tree enforcement
├── sources/            # ONE integrated source per module; registry in sources/__init__.py
├── scoring.py          # GOLD/SILVER/watch + freshness and physical-security program fit
├── enrich/             # Firecrawl/Claude contacts, NCES, ZoomInfo, Salesforce reader + Campaign
├── campaign/           # rich award card: eligibility, snapshot, routing, card, delivery, actions
├── notify/             # Resend transport; the signature takes a Slack id, never an address
├── slack/              # channel-only bot, drip, search/export, tools, Persequor handoff,
│                       #   follow-ups (nudge_*.py), reminders, watchdog, thread scanning
├── roster.py           # the reviewed Slack-id/email/manager map; every external action resolves here
├── territory.py        # state -> rep ownership, and which sources may tag a human at all
├── reminders.py        # rep-requested reminders and the follow-up opt-out register
├── capability_asks.py  # asks Grant had to refuse, so a shipped feature can reopen them
├── user_memory.py      # durable facts a colleague told Grant, with their verbatim words
├── google_sheets.py    # Google Drive/Sheets export integration
├── spreadsheets.py     # local XLSX export generation
├── presentation.py     # factual Slack/export presentation helpers
├── persequor_client.py # durable idempotent draft-intake client and retry worker
└── cli.py              # poll/seed/status/drip/retry/CRM/reconciliation entrypoints
```

Repository-root `tests/` contains pytest coverage and recorded API fixtures; default tests do not
hammer live government servers.

## 3. Data model

`grant_watch/migrations.py` is canonical. The important separation is:

- `source_observations`: immutable evidence payloads and observation hashes.
- `funding_events`: typed event, evidenced date/precision, verification and suppression state.
- `leads`: current projection used by search, ranking, Slack and enrichment.
- durable workflow tables: Slack receipts, search snapshots, export jobs, outreach outbox,
  notification outbox, outcomes/rewards, Salesforce snapshots and CRM action approvals.

Unknown amount, date, enrollment, contact, or CRM state stays unknown. Observation time never becomes
an award date, and an old backfill is suppressed from "new" notifications.

**Dedup rule:** `(source, source_item_id)`. The classic failure here is the SVPP CFDA split — the same
program lives under `16.071` and `16.710`, so `source` must include the CFDA (`usaspending:16.071`) or
the same award reappears/duplicates. See `docs/FINDINGS.md`.

**Future backend parity requirement:** a Postgres migration must preserve every SQLite value and
workflow state. Postgres support is not implemented; test parity rather than assuming it.

**Compatibility debt:** immutable migration 1 still creates `leads.assigned_to`,
`leads.assigned_at`, and an `engagement.kind='claim'` option from the removed ownership workflow.
Runtime code does not use them. The storage maintainer owns their removal through a new forward-only
migration after backup/legacy-upgrade tests; editing the historical migration would break reproducible
upgrades. Until then, these fields must not be presented as product capabilities.

---

## 4. Data sources

`docs/grant_lead_source_inventory.md` records integrated and high-value live-source findings.
`docs/source_inventory/README.md` and its generated CSVs are the nationwide candidate map. Neither
document turns a discovered URL into an integrated poller.

Verified live through 2026-07-14: USAspending prime awards and NSGP subawards, Grants.gov, SAM.gov,
WEBS fetch/parser, California Grants Portal feeds, and the OregonBuys recent-bids feed. NCES district
enrollment/location enrichment was also verified live. OregonBuys returned no security matches during
the live check, so positive-row entity extraction remains needs-testing. See the source inventory for
the per-source evidence and limitations.

Discipline for every source: official API > published PDF > scraped portal; respect robots.txt;
rate-limit; record `verified`/`assumed`/`needs-testing` per source in code and in summaries.

### 4.1 Discovery catalog versus runtime pollers

Source discovery and source integration are deliberately separate:

- `data/source_catalog/sources.csv` stores stable candidate IDs, publisher/jurisdiction scope,
  source kind, access mode, credential environment-variable name, and independent evidence labels.
- `data/source_catalog/coverage_exceptions.csv` records researched gaps and structurally inapplicable
  layers without inventing an endpoint.
- `data/source_catalog/discovery_checks.csv` stores selected Firecrawl query/result evidence and
  content fingerprints linked to a catalog row or coverage exception. Thirty checks from the
  2026-07-15/16 gap-closing passes are currently persisted and validator-backed.
- `grant_watch/firecrawl_client.py` is the typed Firecrawl v1 search transport. It redacts secret-like
  keys, exact credential values in arbitrary response text/keys, and URL query values; it streams into
  a bounded response buffer, classifies retryable and systemic failures, and retains response hashes
  without writing credentials.
- `grant_watch/source_discovery_models.py` owns immutable manifests/checkpoints, deterministic task
  and request identities, and pure paid-attempt state transitions. Schema v2 task identities bind the
  complete target snapshot (namespace, GEOID, state, name, kind, universe vintage) as well as the
  query contract. An `in_flight` attempt is durably written before HTTP; after a crash, retry requires
  the explicit `--retry-indeterminate` choice and preserves the uncertain attempt in the fixed budget.
  Supplying an existing `--batch-id` loads its immutable stored state instead of re-planning it.
- `grant_watch/source_discovery_batch.py` builds deterministic, bounded research batches from
  `not_researched` county, school-district, and incorporated-place tasks. Dry-run performs no network
  or file writes; live runs are rate-limited and stop on systemic authentication or billing errors.
- `grant_watch/source_discovery_store.py` persists immutable manifests and atomically replaced JSONL
  checkpoints under `data/source_catalog/firecrawl_batches/<batch_id>/`. Strict JSON types,
  timestamp/outcome/state validation, request/response hashes, root-wide plus per-batch advisory
  locking, and explicit zero/failure outcomes make batches restartable and auditable. The worker uses
  persisted completion times to enforce its rate window across different batch IDs.
- `grant_watch/coverage_universe.py` pins the official 2025 Census national county Gazetteer by URL,
  byte hash, vintage, and filtered entity count. Explicit GEOID-to-source links live in
  `data/source_catalog/county_source_links.csv`; generated state shards retain a status for every one
  of the 3,144 county-equivalents in the 50 states and DC. The upstream release is documented at
  `https://www.census.gov/geographies/reference-files/2025/geo/gazetter-file.html`.
- County task status is evidence-preserving: a reviewed link becomes `candidate_found`, statewide
  structural evidence may become `not_applicable`, and everything else remains `not_researched`.
  A state-level source or one county example never implies coverage of the other counties.
- `grant_watch/entity_coverage.py` supplies the shared namespaced entity key, many-to-many
  source-link model, deterministic sharding, drift checks, and atomic task replacement used by the
  district and place queues. A source-to-entity relation retains its evidence URL, check date, and
  link method; a scalar source field would lose valid shared-portal and multi-source relationships.
- `grant_watch/school_district_universe.py` pins and validates all four official 2025 Census school
  district layers (elementary, secondary, unified, and administrative-area). Its 13,363 task rows are
  sharded by state and first local GEOID digit; 19 Census "School District Not Defined" rows remain
  structural placeholders rather than research targets.
- `grant_watch/incorporated_place_universe.py` pins the official 2025 place Gazetteer and preserves
  Census functional-status dispositions. Its 32,058 rows are a geographic coverage queue, not a
  deduplicated registry of governments. Statistical and nonfunctioning places are structural; active
  county subdivisions/MCDs are outside this universe. The explicit Brewster, Massachusetts gap
  prevents an active town source from being falsely linked to the statistical Brewster CDP.
- `grant_watch/source_catalog.py` validates those typed records and regenerates the access partitions
  and 50-state-plus-DC matrix in `docs/source_inventory/`.
- `grant_watch/sources/` contains the much smaller set of executable pollers. A candidate reaches this
  layer only after access/terms review, a focused module, recorded fixtures, happy/failure tests, and a
  separately reported live smoke check.

The discovery catalog is durable research memory, not an automatic crawler and not a lead table. It
must never promote `discovered` into `verified` merely because a URL was found.

The current catalog is a manually reviewed snapshot. New selected checks persist a query, retrieval
date, selected rank/title/snippet, deterministic evidence hash, and scraped-content fingerprint.
Historical Firecrawl rows from before this evidence schema remain `needs-testing`. The raw discovery
worker records every result and terminal outcome but has no code path that writes catalog rows,
entity-source links, selected discovery checks, or runtime pollers. A human must review an official
page, verify its access boundary, scrape the selected page, and explicitly promote it. Runtime source
namespace mapping such as `usaspending:16.071` remains deliberately separate and is not automated;
those CFDA/feed-specific namespaces are part of the lead deduplication key.

Raw batch `20260716T004633Z` predates the schema-v2 full-target fingerprint and remains immutable
schema-v1 evidence; its task IDs bind namespace, GEOID, query template, query, result limit, and batch
ID, while its target fields are retained but not independently hash-bound. Schema v1 is accepted only
by read-only loading/validation; checkpoint creation, batch initialization, checkpoint replacement,
and execution require v2.

---

## 5. Grant (the Slack chatbot)

Full spec and the live app's configuration record in `docs/grant_agent.md`. In short:
Grant never posts multi-lead digests. A paced worker surfaces at most one ranked lead or lower-priority
funding bulletin per notification, with strict daily caps. Its initial post is one factual sentence
without links, buttons, menus, CRM detail, or a call to action — **this describes the LEGACY drip
only; the rich card that actually posts in production carries award facts, a contact, typed
Salesforce context and separately labelled links (see §5.2)**. Humans engage only by replying in that
thread or mentioning @Grant in the configured channel; there are no slash commands or DMs. Grant runs
in **Socket Mode** (no public URL). Scheduled CLI workers for polling, drip delivery, outreach retry,
and Salesforce sync expose tested dry-run boundaries. The long-lived Socket Mode listener intentionally
posts replies and has no dry-run flag, so exercise it through offline tests unless a real channel
interaction is explicitly intended. Grant never fabricates a lead, contact, or award figure.

Source-discovery inventory is available through the same natural-language Slack surface. The
`grant_watch/slack/source_status.py` boundary reads validated repository evidence and renders only
aggregates or reviewed catalog fields. Deterministic routing runs before the Anthropic conversation
path, so supported inventory questions cannot fall through to `web_search` or another network tool.
It preserves the distinctions between raw search completion, human review, catalog promotion,
runtime integration, and leads. Raw queries, snippets, hashes, notes, credential metadata, and
payloads never enter the Slack response. The Slack tool is read-only and has no paid-execution
operation; a request to start Firecrawl returns a disabled message until a separately designed admin
approval workflow exists.

---

## 5.1 Salesforce integration (CRM cross-reference)

Grant cross-references each lead against Monarch's Salesforce so it can tell the sales rep what they
already know: *"This district is already an Account — you logged a call 3 days ago"* with a deep link,
or *"No record found — this is net-new."* This turns a raw lead into an actionable, context-aware nudge.

- **Read-only discovery by default.** A bounded worker queries Account, Lead and account-bound open
  Opportunity records and stores status/links locally. Unavailable, partial and ambiguous are distinct
  from no-match; an outage can never label a lead net-new.
- **One narrow write exception: Campaign intake.** A separate credential may create Campaign,
  CampaignMemberStatus, organization-only Lead, and CampaignMember records. It cannot update/delete
  existing CRM records. Every execution requires an immutable Slack preview, one-time nonce, same
  requester/channel, short expiry, and a final button confirmation. The feature flag defaults off
  **in code and is set to 1 in production** (approved 2026-08-10; a human clicked Confirm and 13
  California gold Leads were added to one campaign and read back).
  Complete state/tier requests use a durable parent batch with one isolated child per Campaign.
  The server—not the model or an export—selects and hashes every source row, aggregates all
  contributing lead IDs/grades under an NCES identity when available, freezes exact source and
  organization counts, shares normal search's dead-row exclusion, and refuses the 201st unique
  organization instead of truncating. The manifest separately freezes the exact approved subset;
  click-time verification requires every included organization to map one-to-one to the child
  action before any Salesforce request. Any
  unresolved or ambiguous organization blocks confirmation; an explicitly approved resolved-only
  subset carries an immutable completion mode and remains permanently `partial_by_user`.
  An organization-only Lead is owned by the requesting rep: Grant maps Slack ID to the approved roster
  email, requires exactly one active Salesforce User with that email, and freezes its `OwnerId` in the
  preview. Missing or ambiguous ownership fails before an action is stored; the integration user is
  never an implicit fallback owner.
  Every create request also requires the OAuth instance host to equal the configured HTTPS writer
  host and Salesforce's live Organization ID/sandbox flag to match explicit environment allowlists.
  Each mutating request is durably `in_flight` before HTTP. Returned IDs move to verification-pending,
  and exact CampaignMember readback with the expected non-response status is required before Grant
  reports `added`, records a campaign outcome, or schedules a follow-up. A timeout or missing readback
  becomes `unknown`; Slack retains a requester-bound read-only reconciliation control, and replay
  rechecks the frozen writer org before reading Salesforce and never blindly resubmits.
- **Campaign identity is strict.** Leads require exact Company plus nonblank exact state. Contacts
  bind through exact Account name/ID and `Account.BillingState`; `Contact.MailingState` is not an
  organization identity. Existing Accounts are checked before organization-only Lead creation.
  Blank state, cross-state matches, multiple exact records, unbound NCES rows, and two authoritative
  organizations resolving to one Salesforce member remain blocked.
- **Sandbox for all development.** `test.salesforce.com`, sandbox `monarchdev`
  (`...--monarchdev.sandbox.my.salesforce.com`). Production Salesforce is never touched during dev.
- **Production uses SEPARATE credentials from sandbox** — different org, different Connected App.
  Separate creds give least privilege, independent revocation, and blast-radius isolation (a sandbox
  leak or a dev mistake cannot reach live CRM). Do not reuse the sandbox key in production.
- **Auth:** OAuth 2.0 **client credentials flow** with a dedicated least-privilege integration user
  configured as the Connected App's run-as user — query-focused permission set, not a human admin
  login. Grant implements this flow for both the separate reader and create-only writer clients.
- **Matching must not fabricate.** Exact supporting signals (state/domain/phone and account binding)
  determine confidence. Ambiguous matches remain possible matches and are not used as priority proof.
- Env keys: `SALESFORCE_LOGIN_URL`, `SALESFORCE_SANDBOX_NAME`, `SALESFORCE_MY_DOMAIN_URL`,
  `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET` plus separate `SALESFORCE_WRITE_*` values for
  the create-only Campaign gateway (live), including `SALESFORCE_WRITE_ORG_ID` and
  `SALESFORCE_WRITE_EXPECT_SANDBOX` write-scope attestations (see `.env.example`).

---

## 5.2 Rich award-card campaign (LIVE in production since 2026-08-05)

`grant_watch/campaign/` is a separate product layer selected only when
`GRANT_RICH_CARD_ENABLED` is explicitly truthy. It defaults OFF **in code** and dispatches
to the pre-existing drip unchanged — but it is **set to 1 on the droplet** (Chase's explicit
instruction, 2026-08-05, waiving the five-business-day shadow gate), so in production this is the
path that actually posts. Three `rich_award` cards have been delivered.

Two things measured on production 2026-08-11 that the design above does not imply, and that anyone
reading this section will otherwise assume work:

- **The card's control surface is dead three ways.** `card.py` appends its buttons only when the
  card is not `research_needed`; that mode is unreachable because `leads.nces_website` is 0 of 10,721
  rows with no writer anywhere. Even if a button rendered, `SLACK_WORKSPACE_ID` is absent from the
  droplet environment, so `actions._authorized_snapshot` raises `PermissionError` at its first gate.
  `rich_card_actions` is 0 rows. A human DID once press "Ask Persequor to draft" before the handler
  existed and received nothing at all — one `Unhandled request` line in `bot.log` and no database
  row — so **`rich_card_actions = 0` must never be read as "nobody tried".**
- Cards therefore land with no buttons, and often with **no `@`-mention** (`routing_reason`
  `unassigned`), which is why the follow-up system in §5.3 exists.

- `policy.py` is the pure fail-closed eligibility predicate. Only verified Gold award
  events for NCES-linked districts qualify; precision-safe dates, open spend window,
  completed-run freshness, state/kind provenance, safe links, fresh official contact,
  and fresh complete CRM state are mandatory. No RFP/bulletin fallback exists.
- Migrations 14–24 add completed-run confirmation, rich post/snapshot/action/contact
  state, exact Salesforce owner/activity evidence, forward organization-kind evidence, and
  durable paid-enrichment attempts, and one atomic cross-worker daily-slot claim.
  Historical migrations are unchanged. (Schema has since reached **39**; later migrations add
  campaign batches, nudges, reminders, capability asks, announcements, user memory and the
  ZoomInfo credit ledger.)
- `prepare_worker.py` bounds pre-window contact/activity work. Contact discovery commits
  `in_flight` before possibly paid HTTP and refuses silent restart retry; dry-run makes
  no HTTP call or write. `preparation.py`/`report.py` join persisted evidence and produce
  deterministic PII-free shadow counts.
- `snapshot.py` freezes exact event semantics/amount/evidence, run, organization,
  contact hash/lifecycle, CRM activity, routing, links, and rendering inputs. Each
  evidence version is immutable; the outbox uses a source-qualified stable award key
  plus audience to prevent reposts across versions.
- `routing.py` uses completed-call owner, Account owner, open-Opportunity owner, then
  verified territory. Salesforce email maps exactly through the approved roster and
  channel membership. Nationwide unmapped states remain eligible and explicitly
  unassigned; no owner is guessed.
- `card.py` controls Block Kit and fallback rendering. Untrusted text/URLs are bounded
  and hardened; buttons contain only opaque snapshot IDs; unfurls stay disabled.
- `delivery.py` applies one weekday 10:00–10:45 Pacific slot and an 11:30 hard cutoff
  (`pacing.HARD_CUTOFF_PT`; this said 11:00 and was wrong),
  freezes and reserves before Slack, never blind-retries ambiguity, and writes one
  snapshot-linked `posts` row. Follow-up reminders use the same one-message cap while
  the feature is enabled.
- `actions.py` rechecks workspace/channel/thread/roster and the earliest contact/spend
  expiry, persists
  before Persequor, keeps the exact existing `outreach-request.v1` wire keys, and
  deduplicates retries/double-clicks. Thread replies load complete snapshot context.
  `Not relevant` writes typed feedback and the legacy-visible `not_relevant` status.

  **RETRACTED 2026-08-11:** this used to say card threads "cannot invoke mutable contact/CRM
  tools". That restriction was removed, deliberately. It made a card thread TOOL-DEAD — a rep
  could not search, check Salesforce, enrich, or add to a campaign in the one place leads
  actually arrive — and the frozen-snapshot rationale, while sound, over-reached. There is now a
  single `conversation.respond` with no tool narrowing, which is what makes a card follow-up's
  offer actionable where it lands.

Local commands: `rich-prepare` is preview-only unless `--execute` is explicit;
`rich-shadow` is a read-only deterministic report; ordinary `drip --dry-run` remains
write-free. No command here changes cron or enables the flag.

---

## 5.3 Proactive follow-ups (LIVE; cron `*/15 8-14 * * 1-5`)

Cards and offers were dying in silence: three rich cards, zero engagements; an offer to build a
campaign that nobody answered and nothing noticed, because delivery was treated as completion.
`grant_watch/slack/nudge_*.py` chases unfinished work. Six modules, one responsibility each —
`nudge_sources` (what is outstanding), `nudges` (whether/when/to whom), `nudge_messages` (what it
says), `nudge_promises` (what may be offered), `nudge_silence` (may "nobody answered" be said),
`nudge_variants` (which wording, and did it get a reply).

Eight subject kinds, listed with their exact wording in `docs/grant_message_catalog.md` §4.
Most are threaded replies; **`card_escalated` and `offer_unanswered` are top-level CHANNEL posts**
naming a manager, because their purpose is that somebody sees them (Chase, 2026-08-10, reversing an
earlier DM design).

The load-bearing constraints, each of which cost a real defect to learn:

- **One nudge per subject, ever** — a UNIQUE constraint, not worker logic.
- **Only PERMANENT suppressions may be recorded.** A transient reason writes no row, so an outage
  cannot silently retire a queue. Corollary discovered the hard way: **when adding a suppression
  reason, ask what downstream is waiting on the row it declines to write.** An opted-out owner
  produced a transient suppression whose successor waited forever for that row.
- **Silence is asked of Slack, and may answer "I don't know."** `replied_since` returns
  True/False/**None**, and None is treated exactly like "they replied". It must NOT use
  `slack_event_receipts`, which undercounts. It pages (`has_more`), counts reactions, and treats
  only an explicit DENY list of subtypes as non-human — `file_share`, `thread_broadcast` and
  `me_message` are people talking.
- **Promises are computed from the data**, using the same predicate the consumer requires
  (`contact_status='verified'`), and never promise a SEND: a human approves and Persequor sends,
  and `outreach.sent_at` has no writer, so the database cannot know an email was delivered.
- **An opt-out protects the person being talked ABOUT**, not just the addressee.
- Ordering round-robins across kinds; a card is ranked by the LEAD (tier, money, freshness) because
  a card has no person waiting on it, while every other kind is oldest-person-first.
- The delivery band (08:30–14:30 PT) is COUPLED TO THE CRON and must clear its last tick. Both
  written records of that cron were once wrong; read the crontab.

---

## 5.4 Surfaces that spend money or leave the building

Three surfaces have consequences no test can undo. Each is safe because of its SHAPE, not because a
caller is careful — that distinction is the design, and a refactor that preserves behaviour while
losing the shape has broken it.

- **ZoomInfo** (`enrich/zoominfo*`, migrations for the credit ledger). Contact SEARCH is free and
  returns `hasEmail`/`hasDirectPhone`/`hasMobilePhone` plus do-not-call flags, so a rep can be quoted
  an exact cost before a credit is spent; ENRICH bills 1 credit per returned record. Vendor data
  stores as `vendor_licensed` and **never** `verified`, do-not-call numbers are withheld, and mobile
  is its own column because collapsing it into `Phone` put a mobile where every rep reads a desk
  line. Two live-only facts: Okta refuses a `client_credentials` grant naming no scope, and
  `directPhone` is NOT licensed on this plan — asking for it 400s the whole batch. **The credit
  ledger is per-DATABASE, not a vendor balance**; two databases drawing on one account each believe
  they have the full allowance.
- **Resend email** (`notify/resend_client.py`), a sending-only key scoped to monarchconnected.com.
  **The guardrail is the signature:** `send_to_rep` takes a SLACK USER ID and resolves it through the
  reviewed roster itself. No parameter anywhere accepts an address, so no prompt and no scraped page
  can aim Grant's mail at an outside inbox. A test asserts on the SIGNATURE so a refactor cannot
  loosen it. This is INTERNAL mail to a rep; prospect outreach is Persequor's and is human-approved.
- **Persequor outreach.** Grant builds an `outreach-request.v1` brief and POSTs it; seven were
  accepted 2026-07-15/18. `sent_at` and `response` have **no writer anywhere in the codebase**, so
  the database can confirm handoff accepted and can NEVER confirm an email was sent. Nothing may
  claim delivery. An unreachable endpoint queues locally and says so, falling back to a copyable
  draft — but there is no `outreach-retry` cron line, so a queued row would sit indefinitely.

---

## 6. Deployment & tenant isolation (the security boundary)

Production runs on a **DigitalOcean droplet that is multi-tenant** — it also hosts unrelated tenants
(e.g. `nico`) and an admin account (`chase` / the `monarch` SSH alias used to provision new tenants).

**The rule:** the grants workload gets its OWN isolated tenant, and only the **grants-ops-guardian**
agent operates it, only through a dedicated scoped SSH connection. The guardian may never use admin
access, another tenant's account, `sudo`, or root.

Tenant primitives to provision (Chase runs these once via admin `monarch` access — the guardian never
provisions):

- A dedicated **Unix user** for grants (e.g. `grantwatch`), **no sudo**, confined to its own home.
- A dedicated **SSH keypair** (e.g. `~/.ssh/grants_droplet`) used ONLY for that user.
- The guardian uses the explicit scoped command only: `ssh -i ~/.ssh/grants_droplet -o
  IdentitiesOnly=yes "$GRANTS_DROPLET_USER@$GRANTS_DROPLET_HOST"`. It never relies on a shared SSH
  alias, agent-selected identity, admin login, another tenant, `sudo`, or root.
- A dedicated **Postgres role + database** scoped to grants — the role can reach only its own DB, is
  not a superuser, and cannot see other tenants' data.

The exact provisioning command recipe lives in `.claude/agents/grants-ops-guardian.md` (placeholders for
droplet IP + tenant username). Chase fills those and runs them; then the guardian operates within the
box they define.

### 6.1 How a deploy happens

**Production deploys from `main`, and only by exact hash** (Chase, 2026-08-10). A commit is
deployable only once it is an ancestor of `origin/main`; the guardian asserts that in preflight and
refuses otherwise. Work on a branch, merge, then ship.

Hash-pinning is not ceremony — it has caught a mid-flight commit **three times**, including once
when the repo moved during preflight to a commit that fixed the very command the guardian had been
told to run as its own verification. "Deploy `main`" and "deploy commit X, which is on `main`" differ
the moment somebody pushes mid-sync.

**There is deliberately no deploy script in this repository.** A tracked `deploy_rsync.sh` existed
and was removed on 2026-08-11: it rsynced the laptop WORKING TREE to the droplet with `--delete`, no
ancestry check, no hash pin, no clean-tree check, and a hardcoded droplet IP. A hardened version was
considered and rejected, because the flags were never the safety. The safety is the protocol around
them — backup first with `integrity_check` run against the COPY, a marker plus `find -cnewer` ground
truth, per-file sha256 against the target blobs, an import smoke test BEFORE the listener is killed,
restart verification, and a post-deploy state re-read — plus the part no script can encode: stopping
when a premise turns out to be false. The answer to "how do I deploy?" is **ask the guardian**. An
executable in the repo will eventually get run by someone in a hurry.

---

## 7. Secrets policy

All secrets live in `.env` (git-ignored); `.env.example` is the canonical key-name template and must
contain placeholders only. On the droplet, secrets live in the grants tenant's own environment, never
in the repo or another tenant's space. Never print, echo, or commit a secret. Current integration
families include Slack, Firecrawl, Anthropic, SAM.gov, separate Salesforce reader/writer credentials,
tenant/database settings, Persequor, and Google export credentials. The poll CLI has tested redaction
for SAM, Firecrawl, the Salesforce reader secret, and URL `api_key` parameters. Centralized redaction
for every exception/log path is `needs-testing`; code must therefore avoid logging request headers,
payload credentials, or raw secret-bearing exceptions. Source metadata stores environment-variable
names only.

---

## 8. Testing strategy

- **`pytest`**, typed code throughout.
- **Recorded fixtures** for source parsers (capture one real response per source, commit the fixture,
  test the parser against it) so we can test without hammering live government servers.
- **Live smoke tests** gated behind an explicit flag/env — run manually, never in the default suite.
- **Permanent core live verification:** `grant_watch.live_verification` checks one exact Birmingham
  USAspending award and one same-record role contact on the awardee's exact allowlisted official
  directory. It requires both `GRANT_LIVE_VERIFICATION=1` and `--execute-live`, refuses CI, persists
  no page content, and performs no Slack, CRM, LinkedIn, database, email, or outreach action.
- **Real-model human acceptance:** `tests/test_human_question_acceptance.py` covers the documented
  conversational surface with realistic paraphrases and multi-turn follow-ups. It requires
  `GRANT_LLM_ACCEPTANCE=1`; every tool result is canned, no external action is possible, and the
  server still enforces search confirmation and action-intent gates independently of model wording.
  Deterministic search-plan parsing lives separately in `grant_watch/slack/search_planning.py` so
  explicit date meanings and material filter corrections survive incomplete model tool arguments.
- **Slack human-envelope acceptance:** `tests/test_slack_human_event_path.py` drives human-shaped
  `app_mention` and threaded `message` envelopes through the actual registered Bolt callbacks. It
  verifies channel checks, mention stripping, conversation-thread persistence, progress replacement,
  reply delivery, durable receipt deduplication, and rejection of bot-authored self-mentions.
- **`--dry-run`** exercised in tests for anything that posts to Slack or drafts/sends email.
- Tests never fabricate results; a skipped/blocked test is reported as skipped, not passed.

The repository health gate is documented in `AGENTS.md`. Ruff and Vulture cover lint/dead code;
`python -m grant_watch.health` enforces module/function documentation, annotations, the file-size cap,
and duplicate test-tree detection. The gate also runs canonical pytest, Firecrawl evidence validation,
source-report drift checks, and offline county-task validation.
A clean offline gate is not a substitute for a live source smoke test.

---

## 9. Verification labels (used everywhere)

`verified` = ran it, saw real live data. `assumed` = reasoned, unproven. `needs-testing` = written,
never executed. Every source module, every status report, and every claim to Chase carries one of these.
