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

## Current status (2026-07-15)

- `verified` offline: the canonical `python -m pytest tests -q` target passes 251 tests. The package
  uses seven ordered SQLite migrations, typed evidence/funding models, deduplication, scoring, search,
  export, Slack receipt/reconciliation state, outreach retry state, and Salesforce snapshots.
- `verified` live through 2026-07-14: USAspending prime awards and NSGP subawards, Grants.gov,
  keyed SAM.gov opportunities, WEBS fetch/parser, California Grants Portal, OregonBuys recent-bids,
  NCES district enrichment, and Grant Socket Mode have been exercised. OregonBuys and WEBS returned
  truthful zero security matches during their checks; positive-row entity extraction remains
  `needs-testing`.
- `verified` catalog validation: `data/source_catalog/sources.csv` contains 252 federal, state,
  county, city, school-district, multi-jurisdiction, and portal-family research records. Generated
  public/keyed/account/unknown-access lists and the 50-state-plus-DC coverage matrix live in
  `docs/source_inventory/`. Twelve Firecrawl checks have immutable selected-result evidence in
  `data/source_catalog/discovery_checks.csv`. The pinned 2025 Census county universe tracks 3,144
  county-equivalents in state shards: 53 linked candidates, 15 structural exceptions, and 3,076
  explicitly `not_researched`; most catalog rows remain candidates, not pollers.
- `verified` product behavior: Grant accepts configured-channel mentions and replies in registered
  Grant threads, sends paced individual alerts, and has no digest, DM, slash-command, or ownership
  workflow. Run the bot with `python -m grant_watch.slack.grant`; the dry-run-aware drip entrypoint is
  `python -m grant_watch.cli drip --dry-run`.
- `verified` safeguards in code and tests: seed/live reconciliation, freshness and program-fit
  ranking, immutable source observations, incomplete-run tracking, Slack delivery reconciliation,
  contact evidence gates, idempotent Persequor retry state, read-only Salesforce lookup, and
  create-only Campaign approval state are implemented.
- `needs-testing`: live contact enrichment, a positive OregonBuys/WEBS security row, Persequor live
  round trips, Salesforce sandbox Campaign creation, production scheduling, Postgres parity, and
  tenant-scoped deployment remain unverified. Salesforce Campaign writes stay disabled until explicit
  sandbox approval.
- `assumed` next sequence: characterize high-value catalog candidates, implement them one source per
  module with fixtures and live smoke checks, complete contact-quality testing, then deploy through
  `grants-ops-guardian` only.
