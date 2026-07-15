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
├── CLAUDE.md                 # constitution + mission
├── architectural.md          # this file
├── .env / .env.example       # secrets (real .env git-ignored)
├── requirements.txt
├── grant_watch/              # typed application package
│   ├── migrations.py         # ordered SQLite migrations and durable workflow state
│   ├── sources/              # one official source per module
│   ├── enrich/               # contacts, NCES, Salesforce reader + Campaign gateway
│   └── slack/                # individual proactive alerts and conversation tools
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
├── models.py           # typed Lead, Contact, Outreach, Run (dataclasses/pydantic)
├── db.py               # SQLite repository operations; schema lives in migrations.py
├── sources/            # ONE module per source: usaspending.py, grants_gov.py, pa_pccd.py,
│                       #   mi_cssgp.py, nsgp.py, webs.py, sam_gov.py, ...
├── scoring.py          # GOLD/SILVER/watch + freshness; keyword relevance (Claude pass)
├── enrich/             # Firecrawl/Claude contacts, NCES, Salesforce reader and Campaign actions
├── slack/              # grant.py (bot), drip.py (single alerts), persequor.py (handoff)
├── cli.py              # entrypoints; --dry-run everywhere that posts/sends
└── tests/              # pytest; recorded API fixtures (no live gov hammering)
```

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

---

## 4. Data sources (summary — full map in `docs/grant_lead_source_inventory.md`)

Verified live through 2026-07-14: USAspending prime awards and NSGP subawards, Grants.gov, SAM.gov,
WEBS fetch/parser, California Grants Portal feeds, and the OregonBuys recent-bids feed. NCES district
enrollment/location enrichment was also verified live. OregonBuys returned no security matches during
the live check, so positive-row entity extraction remains needs-testing. See the source inventory for
the per-source evidence and limitations.

Discipline for every source: official API > published PDF > scraped portal; respect robots.txt;
rate-limit; record `verified`/`assumed`/`needs-testing` per source in code and in summaries.

---

## 5. Grant (the Slack chatbot)

Full spec and the live app's configuration record in `docs/grant_agent.md`. In short:
Grant never posts multi-lead digests. A paced worker surfaces at most one ranked lead or lower-priority
funding bulletin per notification, with strict daily caps. Individual lead alerts offer [Draft email],
[Mark contacted], [Snooze], and [Bad lead]; human-approved outreach is handed to @Persequor. Grant runs
in **Socket Mode** (no public URL). Everything that posts or drafts honors `--dry-run`. Grant never
fabricates a lead, contact, or award figure.

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

All secrets in `.env` (git-ignored); template in `.env.example`. On the droplet, secrets live in the
grants tenant's own environment, never in the repo, never in another tenant's space. Never print, echo,
or commit a secret. The database URL, Slack tokens, Firecrawl key, Anthropic key, and SAM key are the
only secrets today.

---

## 8. Testing strategy

- **`pytest`**, typed code throughout.
- **Recorded fixtures** for source parsers (capture one real response per source, commit the fixture,
  test the parser against it) so we can test without hammering live government servers.
- **Live smoke tests** gated behind an explicit flag/env — run manually, never in the default suite.
- **`--dry-run`** exercised in tests for anything that posts to Slack or drafts/sends email.
- Tests never fabricate results; a skipped/blocked test is reported as skipped, not passed.

---

## 9. Verification labels (used everywhere)

`verified` = ran it, saw real live data. `assumed` = reasoned, unproven. `needs-testing` = written,
never executed. Every source module, every status report, and every claim to Chase carries one of these.
