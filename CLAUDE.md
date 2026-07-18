# CLAUDE.md — grants_agent ("Grant Lead Watcher")

**Owner:** Chase Gonzales, Monarch Connected (Verkada reseller — cameras + access control, SLED focus).
**Repo:** `git@github.com:SFBAYKID/grants_agent.git`
**You:** Claude Code working in this repo. This file is your standing briefing and your rules. Read
`architectural.md` before designing anything, and `docs/` before adding a data source.

---

## THE CONSTITUTION (non-negotiable — these override convenience, deadlines, and "finish the work")

1. **Never lie or fabricate data. Ever.**
   - Never invent API output, a success message, a contact, an email, a phone number, an award amount,
     or a test result. If you did not run it, say so. If a poller was not verified against live data,
     say so.
   - **Label every claim** you report as one of: `verified` (you ran it and saw real data),
     `assumed` (reasoned but unproven), or `needs-testing` (written but never executed).
   - If a contact or email is **not found**, record `contact_status='not_found'` and let a human
     research it. **Never guess an email address.** A fabricated lead is worse than no lead.
   - Tell **owner** the honest truth.  He would rather be told the truth then you fabricated the truth.

2. **Type-annotate and note everything.**
   - Every function has full type annotations (params + return). No untyped `dict` blobs passed around —
     use typed models (dataclasses / pydantic). No bare `Any` without a one-line reason comment.
   - Every module has a header comment (what/why). Every function has a docstring saying what it does
     and why. Comment non-obvious logic, especially parser selectors and API quirks.

3. **Code is not done until it is written AND tested.**
   - Tests run with `pytest`. Cover happy paths AND failure modes (empty results, API 500s, malformed
     HTML/PDF, pagination, dedup collisions).
   - Distinguish "tests pass" from "verified against live data" — both matter; neither substitutes for
     the other. Do not claim a poller works until it has returned real data from the live source.

4. **File size cap: 0–1000 lines MAX per file — including `.md` files.**
   - Split by responsibility *before* a file gets close to the cap. One data source per module, small
     and focused. If a doc is growing past ~800 lines, split it and link the parts.

5. **No dead code. Remove one-time / throwaway code.**
   - If you write a script for a one-time job (a backfill, a data pull, a diagnostic), delete it when
     the job is done so it does not bloat the codebase. No commented-out blocks, no orphan scripts,
     no `TODO` without an owner and a description, no stray debug prints.

6. **Report to Chase periodically — program size + dead-code sweep.**
   - Occasionally (at phase boundaries or when asked) report: total lines of code, file count, the
     largest files, and anything approaching the 1000-line cap. In the same report, flag any code
     that appears unused / unreferenced (dead code) and propose removing it.

7. **Push often.**
   - Remote `origin` is already configured (`git@github.com:SFBAYKID/grants_agent.git`). Commit at every
     working increment; push after. Small, honest commits. Never commit `.env`, `*.db`, or any secret.

8. **Secrets live in `.env` only — never in code or git.** `--dry-run` on anything that posts to Slack
   or sends/drafts email. See `.env.example` for the key list.

9. **Tenant isolation is sacred.** The production server is a **DigitalOcean droplet shared with
   unrelated tenants**. Only the **grants-ops-guardian** agent touches that server, and only through the
   dedicated scoped SSH connection for the grants tenant — **never** the admin (`monarch`) access,
   **never** another tenant (`nico`, etc.), **never** `sudo`/root. See `architectural.md` and
   `.claude/agents/grants-ops-guardian.md`.

10. **Outreach is honest and human-approved.** Personalized ≠ deceptive: identify Monarch Connected as
    sender, no impersonation or pretexting, include opt-out. A human approves in Slack **before** any
    email is sent; Grant proposes, a human clicks approve, then @Persequor sends.

---

## Mission

Build a **weekly grants checker** that:
1. Finds schools/cities that **are getting or just got** government funding for physical security.
2. Finds **who runs technology/the funding** at the awardee (Technology/IT Director, Facilities/Operations
   Director, Superintendent, Business Manager — title varies by district size) via **public sources**.
3. Surfaces opportunities through **Grant**, a human-centric Slack chatbot, on a **weekly cron**.
4. From Slack, offers to **draft/send outreach**, handing approved sends to the existing **@Persequor**
   Slack agent.
5. Stores all leads in **local SQLite first**, then migrates to **DigitalOcean Postgres** once proven.
6. If lead quality is good → **expand to more states** (by config, not code).

**Lead grading (Chase's definitions):**
- 🥇 **GOLD** — entity applied and **just got** security funding (award announced, spend window open,
  ideally < 12 months old — after ~a year they likely have vendors in place).
- 🥈 **SILVER** — entity is applying / has an open RFP for access control or cameras.
- **Freshness is everything.** An award from last month beats one from two years ago.
- When keyword-scoring is ambiguous, keep the lead as `watch` rather than dropping it.

---

## The agents in this repo

- **Grant** — the Slack chatbot persona (the product). Talks to humans and to other Slack agents
  (@Persequor). Posts paced individual lead alerts, runs the approve-to-email flow. Honest, human-in-the-loop,
  never fabricates. Spec + live app config: `docs/grant_agent.md` (Slack app provisioned 2026-07-13;
  tokens in `.env`).
- **grants-ops-guardian** (`.claude/agents/`) — the ONLY thing allowed to operate the DigitalOcean
  droplet, and only via the scoped grants SSH. Use it for any server / production-database operation.
- **architectural-critic** (`.claude/agents/`) — stress-tests plans and designs before implementation;
  hunts edge cases, parser drift, failure modes, and testing gaps. Use it before committing to a design.

These two agents are **project-scoped** (they live in this repo, not your global config) so they cannot
affect Chase's other projects.

---

## Working agreements

- In **every** summary you give Chase, mark each claim `verified` / `assumed` / `needs-testing`.
- Prefer **official APIs > published PDFs/pages > scraping portals.** Respect `robots.txt`; sleep
  between requests — these are government servers, do not hammer them.
- Small commits per working increment; a `--dry-run` flag on anything that posts to Slack or drafts email.
- Read `docs/source_inventory/README.md`, `data/source_catalog/sources.csv`, `docs/FINDINGS.md`, and
  `docs/grant_lead_source_inventory.md` before touching data sources. The generated inventory records
  nationwide candidates; the legacy findings record live integrations and gotchas (e.g. SVPP is split
  across CFDA `16.071` **and** `16.710`; query one and you silently lose most leads).

## Current status (2026-07-18)

- `verified` offline: `python -m pytest tests -q` passes 517 tests (71 skipped live-marked). The
  package uses nine ordered SQLite migrations, typed evidence/funding models, deduplication, scoring,
  guided search with zero-result relaxation hints, per-record verification links, export, Slack
  receipt/reconciliation state, outreach retry state, and Salesforce create-only writes.
- `verified` live 2026-07-17→18, full-workflow campaign in Slack (runs 1–7 plus Chase's realism
  passes): natural asks ("find me schools in Texas") search immediately and answer with a plain-words
  grade split, names, and a per-record source link on every row; open-ended asks get ONE scoping
  question; zero results return concrete widen/broaden counts, never a dead end. Contact lookups
  escalate site person → LinkedIn decision-maker → verified org mailbox before an honest none-found.
  Full person Leads (address/industry/enrollment/LinkedIn/record type) with a completed activity Task
  and a Lightning ContentNote were created through the bot's preview→button→native-confirm flow and
  SOQL-verified (Wally Rakestraw #7845, Jake-Rawlinson-backed Commerce ISD staged). Persequor
  drafted and — on a tapped Send — delivered the test-mode email to chase@ (Gmail-verified). Pronoun
  traps, duplicate-record guard, compression attacks, and outreach refusals all held server-side.
- `verified` live drip loop 2026-07-18: the real engine posted the paced one-line nugget, refused a
  repost on the next tick, and the contextual follow-up ("who should I talk to about that award you
  just posted?") returned a verified contact plus Salesforce state. Bulletin relevance is now
  precision-first (a live health-sector miss was fixed same-day). Backfilled award events are
  deliberately suppressed from drip, so the imported gold backlog only surfaces via search/polls —
  an open product decision, not a bug.
- `verified` deployment: the droplet tenant `grantwatch` runs main (rsync + revision stamp + restart
  recipe in guardian memory); cron is Pacific-time — 5-min keepalive, 30-min drip 05:00–17:30 PT
  weekdays, daily 07:00 PT poll — six live sources, zero incomplete runs, ~9.4k new leads in the
  week to 2026-07-18. Grant's replies follow hard formatting rules: paragraph spacing, no internal
  identifiers, no emoji in alerts. Orphaned progress spinners are swept and finalized at bot boot.
- `verified` catalog validation: `data/source_catalog/sources.csv` contains 270 federal, state,
  county, city, school-district, multi-jurisdiction, and portal-family research records. Generated
  public/keyed/account/unknown-access lists and the 50-state-plus-DC coverage matrix live in
  `docs/source_inventory/`. Thirty Firecrawl checks have immutable selected-result evidence in
  `data/source_catalog/discovery_checks.csv`. The pinned 2025 Census county universe tracks 3,144
  county-equivalents in state shards: 56 linked candidates, 15 structural exceptions, and 3,073
  explicitly `not_researched`; most catalog rows remain candidates, not pollers.
- `verified` geography queues: four pinned 2025 Census school-district layers track 13,363 entities
  with 66 linked candidates, 19 structural placeholders, and 13,278 `not_researched`. The pinned
  incorporated-place layer tracks 32,058 Census places with 14 linked candidates, 12,587 structural
  non-government rows, and 19,457 `not_researched`. These are geography queues rather than counts of
  unique governments; active county subdivisions/MCDs remain a separate `needs-testing` universe.
- `verified` raw discovery evidence: Firecrawl batch `20260716T004633Z` stores 27 completed search
  tasks, 27 attempts, and 126 returned results without credentials. Eight manually reviewed official
  pages were promoted; raw batch results never promote catalog rows or runtime pollers automatically.
- `verified` product behavior: Grant accepts configured-channel mentions and replies in registered
  Grant threads, sends paced individual alerts, and has no digest, DM, slash-command, or ownership
  workflow. Run the bot with `python -m grant_watch.slack.grant`; the dry-run-aware drip entrypoint is
  `python -m grant_watch.cli drip --dry-run`.
- `verified` offline Slack discovery UI: natural-language source-inventory, state/layer coverage,
  reviewed-source, and recent-batch questions return validated read-only evidence without Anthropic,
  web search, raw payloads, or paid Firecrawl execution. Live configured-channel interaction is
  `needs-testing`.
- `verified` live on 2026-07-16: the opt-in read-only core verifier matched Birmingham Community
  Charter High School's exact $500,000 USAspending award and Vic Chalabian's IT Systems Manager role
  within one official staff-directory record. This does not verify a personal email, LinkedIn profile
  ownership, Salesforce state, or outreach. Run it only with the documented double opt-in.
- `verified` real-model acceptance on 2026-07-16 (updated 2026-07-18): realistic human scenarios
  pass with write-free canned outcomes. Server-side gates prevent date-filter loss, pronoun-only
  contextual tool calls, outreach refusals becoming approvals, accidental bad-lead/snooze actions,
  false outreach success, repeated paid/slow tool execution, and typed confirmation from silently
  executing Salesforce writes. NOTE (2026-07-18 redesign, Chase's UX rule): read-only searches with
  any state/org/city/entity anchor now run IMMEDIATELY without a confirmation round-trip; only fully
  open-ended asks get one scoping question. Approval gates remain on paid contact enrichment,
  Salesforce writes, and email.
- `verified` offline Slack ingress acceptance: human-shaped mention and plain threaded follow-up
  envelopes traverse Grant's registered Bolt handlers, produce correct source answers, persist
  delivered receipts, deduplicate redelivery, and reject bot self-mentions. Remote Socket Mode receipt
  from a genuine Slack user remains separate live evidence.
- `verified` safeguards in code and tests: seed/live reconciliation, freshness and program-fit
  ranking, immutable source observations, incomplete-run tracking, Slack delivery reconciliation,
  contact evidence gates, idempotent Persequor retry state, read-only Salesforce lookup, and
  create-only Campaign approval state are implemented. Organization-only Salesforce Leads freeze the
  requesting rep's exact active-user `OwnerId` in the preview and fail closed instead of falling back
  to the integration user.
- `verified` live in the `monarchdev` Salesforce sandbox on 2026-07-16: one synthetic
  organization-only Lead was created and read back with Chase's exact active `OwnerId` and roster
  email, blank person/contact fields, exact organization fields, and a unique provenance marker. The
  record remains in the sandbox; this does not verify Campaign or production writes.
- `needs-testing`: a positive OregonBuys/WEBS security row, Salesforce sandbox Campaign
  creation/membership, Salesforce production writes, Postgres parity, and the drip-thread reply path
  from a genuine phone client. Salesforce Campaign writes stay disabled until explicit sandbox
  approval; all sandbox test records await Chase's delete/keep decision (Ben Bayle, Wally Rakestraw,
  Richard Moline, ZZ FLS Probe).
- `assumed` next sequence: decide the gold-backlog surfacing product question, characterize
  high-value catalog candidates one source per module with fixtures and live smoke checks, then keep
  operating the droplet only through `grants-ops-guardian`.
