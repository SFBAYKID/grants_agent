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
implemented. Production scheduling and live integration smoke tests remain separate deployment work.

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
│   ├── enrich/               # contacts, NCES, Salesforce reader + Campaign gateway
│   └── slack/                # individual proactive alerts and conversation tools
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
├── migrations.py       # seven ordered migrations; never mutate old migrations in place
├── source_catalog.py   # typed candidate catalog, evidence validation, generated reports
├── source_discovery.py # immutable Firecrawl search and scrape fingerprints
├── coverage_universe.py # pinned Census county universe and per-entity research status
├── health.py           # docs/annotations/line-cap/nested-test-tree enforcement
├── sources/            # ONE integrated source per module; registry in sources/__init__.py
├── scoring.py          # GOLD/SILVER/watch + freshness and physical-security program fit
├── enrich/             # Firecrawl/Claude contacts, NCES, Salesforce reader and Campaign actions
├── slack/              # channel-only bot, drip, search/export, tools, Persequor handoff
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
without links, buttons, menus, CRM detail, or a call to action. Humans engage only by replying in that
thread or mentioning @Grant in the configured channel; there are no slash commands or DMs. Grant runs
in **Socket Mode** (no public URL). Scheduled CLI workers for polling, drip delivery, outreach retry,
and Salesforce sync expose tested dry-run boundaries. The long-lived Socket Mode listener intentionally
posts replies and has no dry-run flag, so exercise it through offline tests unless a real channel
interaction is explicitly intended. Grant never fabricates a lead, contact, or award figure.

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
  requester/channel, short expiry, and a final button confirmation. The feature flag defaults off.
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
  the disabled Campaign gateway (see `.env.example`).

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
