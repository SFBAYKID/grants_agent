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
- Read `docs/FINDINGS.md` and `docs/grant_lead_source_inventory.md` before touching data sources —
  they record what is already verified and the gotchas (e.g. SVPP is split across CFDA `16.071` **and**
  `16.710`; query one and you silently lose most leads).

## Current status (2026-07-13, end of day)

- **Phase 1 built and `verified` live.** The `grant_watch/` package (typed models, 4-table schema,
  per-source modules, CLI with `--dry-run`) replaced the v1 single-file scaffold, which was deleted.
  Run it: `python -m grant_watch.cli poll|seed|status`. Tests: `python -m pytest` (18 passing, on
  recorded fixtures in `tests/fixtures/`).
- Per-source verification: **usaspending** `verified` (SVPP filter fixed — unfiltered 16.710 was 96%
  non-school noise; pagination added; 4 states polled); **grants.gov** `verified`; **sam.gov**
  `verified` with Chase's key; **webs** fetch+parse `verified`, but entity extraction from group-header
  rows is `needs-testing` until a real security bid appears on the page (capture-day HTML verifiably
  contained zero security keywords, so 0 matches was correct).
- DB seeded: 75 CSV GOLD + 75 live GOLD SVPP awards + 96 expired-window watch + 153 grants.gov
  signals + 4 SILVER RFPs. Dedup `verified` (repeat poll → 0 new).
- **Phase 3 built and `verified` live.** `grant_watch/slack/` — paced individual proactive alerts and
  a Grant bot that accepts only configured-channel @mentions and proactive-thread replies. Slash
  commands, DMs, menus, and buttons on initial alerts are removed. A historical 16-lead digest was
  posted on 2026-07-13; digest posting has since been removed globally. Socket Mode boot is `verified`.
  Run the bot:
  `python -m grant_watch.slack.grant`. Proactive cron target: `python -m grant_watch.cli drip`.
  Outstanding: invite @Grant to the alert channel (posting works via chat:write.public, but thread
  reads need membership); set PERSEQUOR_USER_ID in .env so handoffs ping.
- **Post-launch fixes (same day, `verified`):** seed-vs-live duplicate reconciliation (75 superseded
  CSV rows retired; expiring bucket 34→17) and the proactive quality gate (`scoring.lead_score` —
  freshness × dollars × program camera-fit; the highest-ranked lead wins, watch never surfaces).
- **Removed from product:** claim/ownership/dibs workflow. Rep interest now leads to Salesforce
  lookup, contact research, or a Persequor draft. Territory routing remains future design.
- **Implemented but still gated:** Grant↔Persequor draft intake and Salesforce cross-referencing;
  Salesforce Campaign writes stay disabled until the sandbox workflow is explicitly approved.
- Next: Phase 2 (contact enrichment — Firecrawl + Claude extraction, not_found never fabricated),
  then cron on the droplet (Phase 4 tenant).
