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
        leads DB  ──►  contact enrichment (Firecrawl crawl + Claude extraction)
            │                 │  never fabricate — not_found is a valid outcome
            ▼                 ▼
   weekly cron ──► Grant (Slack digest + buttons) ──► human approves ──► @Persequor sends email
```

Phasing (see `CLAUDE.md` mission): Phase 1 pollers + local SQLite → Phase 2 contact enrichment →
Phase 3 Grant/Slack/cron → Phase 4 DigitalOcean Postgres → Phase 5 state expansion.

---

## 2. Repository layout

**Current (v1 scaffold, consolidated from Desktop):**

```
grants_agent/
├── CLAUDE.md                 # constitution + mission
├── architectural.md          # this file
├── .env / .env.example       # secrets (real .env git-ignored)
├── requirements.txt
├── grant_watch.py            # v1 poller scaffold — NEVER run end-to-end; to be refactored
├── data/svpp_active_awards_CA_MI_PA_WA.csv   # 75 verified GOLD seed leads
├── docs/FINDINGS.md
├── docs/grant_lead_source_inventory.md
├── docs/grant_agent.md       # Grant (Slack bot) spec + live app config record
├── assets/                   # Grant logo (owl) — also set as the Slack app icon
└── .claude/agents/           # project-scoped agents (grants-ops-guardian, architectural-critic)
```

**Target package (when we build the program — one responsibility per module, each well under the
1000-line cap):**

```
grant_watch/
├── __init__.py
├── models.py           # typed Lead, Contact, Outreach, Run (dataclasses/pydantic)
├── db/                 # schema, migrations, SQLite + Postgres backends
├── sources/            # ONE module per source: usaspending.py, grants_gov.py, pa_pccd.py,
│                       #   mi_cssgp.py, nsgp.py, webs.py, sam_gov.py, ...
├── scoring.py          # GOLD/SILVER/watch + freshness; keyword relevance (Claude pass)
├── enrich/             # firecrawl.py (crawl), extract.py (Claude staff-directory extraction)
├── slack/              # grant.py (bot), digest.py (message formatting), persequor.py (handoff)
├── cli.py              # entrypoints; --dry-run everywhere that posts/sends
└── tests/              # pytest; recorded API fixtures (no live gov hammering)
```

`grant_watch.py` (the single-file v1) is kept as reference until the package replaces it, then deleted
(no dead code).

---

## 3. Data model (canonical — supersedes v1's flat `seen` table)

```sql
CREATE TABLE leads (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- 'usaspending:16.071', 'pccd_pdf', 'webs', ...
  source_item_id TEXT NOT NULL,
  lead_grade TEXT CHECK(lead_grade IN ('gold','silver','watch')),
  entity_name TEXT NOT NULL,
  entity_type TEXT,                -- district, city, nonpublic_school, nonprofit
  state TEXT, county TEXT,
  program TEXT,                    -- SVPP, NSGP, CSSGP, PCCD, STOP, RFP:<platform>
  amount REAL,
  funds_start DATE, funds_end DATE,
  detail_url TEXT,
  raw_json TEXT,
  first_seen TIMESTAMP, last_seen TIMESTAMP,
  status TEXT DEFAULT 'new',       -- new, surfaced, contacted, snoozed, replied, opportunity, dead
  status_note TEXT,                -- human feedback, e.g. the [Bad lead] reason (feeds scoring)
  UNIQUE(source, source_item_id)   -- the dedup key
);
CREATE TABLE contacts (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER REFERENCES leads(id),
  name TEXT, title TEXT, email TEXT, phone TEXT,
  source_url TEXT, confidence TEXT CHECK(confidence IN ('high','medium','low')),
  contact_status TEXT DEFAULT 'unverified'   -- unverified, verified, not_found (NEVER fabricate)
);
CREATE TABLE outreach (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER, contact_id INTEGER,
  channel TEXT, draft TEXT, approved_by TEXT,   -- approved_by is required before sent_at is set
  sent_at TIMESTAMP, response TEXT
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY, started TIMESTAMP, finished TIMESTAMP,
  source TEXT, items_seen INT, items_new INT, errors TEXT
);
```

**Dedup rule:** `(source, source_item_id)`. The classic failure here is the SVPP CFDA split — the same
program lives under `16.071` and `16.710`, so `source` must include the CFDA (`usaspending:16.071`) or
the same award reappears/duplicates. See `docs/FINDINGS.md`.

**Schema parity across backends:** SQLite (Phase 1) and Postgres (Phase 4) use the same logical schema.
The Postgres migration must preserve every value; test parity, do not assume it.

---

## 4. Data sources (summary — full map in `docs/grant_lead_source_inventory.md`)

Verified live (2026-07-13): USASpending prime awards + **subawards** (NSGP end-recipients), Grants.gov
`search2`, PA PCCD award PDFs, WEBS bid calendar. Blocked/unwired: SAM.gov (needs Chase's key), MI CSSGP
PDFs, FEMA NSGP state lists, COPS autumn announcements, SSE 84.184A (new $93M program → district lead
waves early 2027). Each source is one module in `sources/`, each labeled with its verification status.

Discipline for every source: official API > published PDF > scraped portal; respect robots.txt;
rate-limit; record `verified`/`assumed`/`needs-testing` per source in code and in summaries.

---

## 5. Grant (the Slack chatbot)

Full spec and the live app's configuration record in `docs/grant_agent.md`. In short:
Grant posts the weekly digest (new GOLD/SILVER leads, expiring-window alerts), offers per-lead buttons
([Draft email] [Mark contacted] [Snooze] [Bad lead]), and on human approval hands the send to @Persequor.
Grant runs in **Socket Mode** (no public URL). Everything that posts or drafts honors `--dry-run`. Grant
never fabricates a lead, contact, or award figure.

---

## 5.1 Salesforce integration (CRM cross-reference)

Grant cross-references each lead against Monarch's Salesforce so it can tell the sales rep what they
already know: *"This district is already an Account — you logged a call 3 days ago"* with a deep link,
or *"No record found — this is net-new."* This turns a raw lead into an actionable, context-aware nudge.

- **Read-mostly, query-first.** The integration primarily runs SOQL queries (match Account/Lead/Contact
  by entity name, domain, address; read recent Activities/Tasks) and returns record links. Any write-back
  (e.g. creating a Lead) is a later, explicitly-scoped decision — not assumed.
- **Sandbox for all development.** `test.salesforce.com`, sandbox `monarchdev`
  (`...--monarchdev.sandbox.my.salesforce.com`). Production Salesforce is never touched during dev.
- **Production uses SEPARATE credentials from sandbox** — different org, different Connected App.
  Separate creds give least privilege, independent revocation, and blast-radius isolation (a sandbox
  leak or a dev mistake cannot reach live CRM). Do not reuse the sandbox key in production.
- **Auth:** OAuth 2.0 **JWT Bearer flow** (server-to-server, no interactive login) with a **dedicated
  least-privilege integration user** — query-focused permission set, not a human admin login. This suits
  the weekly cron. Username-password + security-token is a fallback for quick local testing only.
- **Matching is fuzzy and must not fabricate.** Entity-name matching across gov data and CRM is
  imperfect; when uncertain, Grant says "possible match" with the link and lets the human confirm —
  it never asserts a match it cannot support, and never invents a record or a "last contacted" date.
- Env keys: `SALESFORCE_LOGIN_URL`, `SALESFORCE_SANDBOX_NAME`, `SALESFORCE_MY_DOMAIN_URL`,
  `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET`, `SALESFORCE_USERNAME`, `SALESFORCE_JWT_KEY_PATH`
  (see `.env.example`).

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
- A dedicated **`~/.ssh/config` Host alias** (e.g. `grants`) → the droplet IP, `User grantwatch`,
  `IdentityFile ~/.ssh/grants_droplet`. The guardian uses ONLY `ssh grants`.
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
