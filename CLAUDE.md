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

## Current status (2026-07-21)

- `verified` 2026-07-20 PRODUCTION CUTOVER (guardian + read-only API): Grant is LIVE on the
  production channel `C01DGT9D11D` (monarch-cloud-team-vekada, `is_member:true`), running `15263d2`
  with migration 13 applied. Salesforce is PRODUCTION — read verified live, `verify_write_scope`
  PASSES (IsSandbox=False, Org `…8EAM`, EXPECT_SANDBOX=0); writes are ARMED but gated per-record by
  `verify_write_scope` + human Slack approval, and NO production insert has fired yet. Writer OAuth
  creds fall back to the reader's (aa09dca); the two write-SAFETY vars keep no fallback. Crons
  (Pacific): drip every 30 min 04:00–17:30 weekdays, poll 07:00 weekdays, keepalive 5-min. The
  playground `C0B02721MNK` is now quiet (multi-channel dev support not yet built). architectural-critic
  sweep of aa09dca: zero critical code bugs. LOCAL Mac env WORKS again (Python 3.13.14, venv intact) —
  the earlier "Homebrew removed python@3.13" note is stale.
- `verified` 2026-07-21 duplicate-lead fix (Chase-authorized; he ran the prod write himself after the
  permission gate blocked the guardian twice — the guardian correctly stopped both times rather than
  improvising a transport). ROOT CAUSE: `upsert_lead` identified a lead ONLY by
  `(source, source_item_id)`, so `eabf6e5`'s legitimate change to the `rfp_item_id` formula orphaned
  every row stored under the old shape and the next poll re-inserted them — Grant had an exact repeat
  of the PA DOC card queued for 07-22. FIXED in two halves: (a) prod data reconciled — leads 9564/9534
  and their `funding_events` deleted, 9533 re-keyed onto the current key, keeping its post history;
  (b) `db._adopt_drifted_lead` now re-keys a drifted row IN PLACE instead of duplicating it, gated on
  source + detail_url + ORGANIZATION (URL alone fused two different cities in the search fixtures).
  The code guard alone would NOT have repaired the existing duplicate — the data fix was load-bearing.
  NOTE: Chase chose to KEEP the two `source_observations` rows, so `PRAGMA foreign_key_check` now
  reports 2 orphaned rows PERMANENTLY. That is the decision, not damage; `integrity_check` is `ok`.
  Do not "fix" it. Backup retained: `/home/grantwatch/grant_watch.db.bak.20260721T075909Z`.
- `verified` 2026-07-21 `db.py` split (it crossed the 1000-line cap): `db_common.py` holds the shared
  row-shape fragments and `_now`; `db_engagement.py` holds human signals + the drip-selection queries.
  Both are re-exported from `db.py`, so every `db.<name>` call site is unchanged.
- `needs-testing` 2026-07-21 drip TIMING, the likeliest cause of low team engagement: `in_window()`
  opens at 7am ET = **4:00 AM Pacific** and `POST_PROBABILITY=0.45` per 30-min tick, so with
  `DAILY_CAP=1` the single daily card is ~95% likely to be spent before 6 AM PT — hours before the
  Pacific team logs on, with nothing left for the rest of the day. Monday's landed 04:30 PT. Proposed
  fix (not yet approved): open the window at 08:00 PT. Chase has decided the cap STAYS at 1/day.
  CONFIRMED against prod cron.log 2026-07-22: the last three cards landed 04:30 / 04:00 / 05:00 PT,
  each followed by 24–26 consecutive `skip: daily cap reached (1)` ticks.
- `verified` 2026-07-22 drip TIMING FIXED (Chase approved the design, then asked to try ~10:45 PT).
  Root cause was the flat `POST_PROBABILITY=0.45` roll on every 30-min tick from 04:00 PT: per-tick
  rolling front-loads and CANNOT be tuned to land late. Replaced with a per-day SLOT — one target
  time drawn inside a Pacific band, seeded by `(date, channel)` so every tick of a day agrees on it
  (a per-tick reroll would move the goalpost and bring the front-loading straight back). The card
  posts at the first tick at/after the target. `POST_PROBABILITY`/`DAILY_AIM` and the `rng` argument
  to `pacing_ok`/`should_post`/`run_drip` are GONE (rule 5, no dead code). Band defaults to
  10:00–11:30 PT and is env-tunable WITHOUT a deploy via `DRIP_SLOT_START_PT` / `DRIP_SLOT_END_PT`
  ("HH:MM", Pacific) — an unset var is silent, a malformed one warns once and falls back, and an
  inverted band collapses to a single slot rather than silencing the card. `in_window()` is
  UNCHANGED (still 7am ET–5pm PT) and now only acts as the outer guard. Urgent/exceptional cards
  bypass the slot. Simulated: Mon 11:24, Tue 10:14, Wed 10:04, Thu 11:01, Fri 10:09 PT.
- `verified` 2026-07-22 TWO CRITICAL DEFECTS found by architectural-critic review, both REPRODUCED
  against a real DB. CORRECTION (Chase, 2026-07-22): an earlier version of this entry said "both now
  fixed" — that OVERCLAIMED. `85295d7` closed one member of the wedge CLASS (the reserved-but-
  unconfirmed path) and left another live: the renderers raise BEFORE any reservation exists, so
  nothing recorded the failure and the same lead was re-picked every tick. Both halves are fixed as
  of the follow-up commit below. Rule 1 applies to status claims as much as to lead data.
  (C1) PERMANENT SILENT WEDGE: an ambiguous Slack send (5xx/ratelimit/timeout) leaves
  `notification_outbox` in state 'unknown' and is deliberately never retried — but the lead stayed
  `status='new'`, absent from `posts`, and still the winner of `_best_nugget`'s deterministic `max()`
  over a STATIC pool. Every later tick re-picked it, `reserve_notification` returned None on the
  existing delivery_key, and `run_drip` returned early BEFORE the RFP and bulletin tiers. One
  ambiguous send silenced the WHOLE product forever, behind a benign `skip:` line and exit code 0 —
  and over ~250 posts/year that is near-certain. FIX: `nugget_candidates`, `rfp_candidates` and
  `bulletin_candidates` now also exclude leads present in `notification_outbox`, so the never-blind-
  retry guarantee holds (that lead stays skipped) while the queue ADVANCES.
  (C2) WRONG-REP TAGGING: `rfp_aggregator._row_state` infers state by searching row prose for five
  state NAMES, so "Oregon City Schools, Ohio"→OR, "City of California, Missouri"→CA, "1600
  Pennsylvania Avenue NW"→PA. `RFP_DISCOVERY_ENABLED` IS live in prod (the 07-22 poll logged
  "[Security RFP discovery] 3 items"), so territory tagging would have pinged a rep's phone claiming
  they own another rep's deal. FIX: `territory.VERIFIED_STATE_SOURCES` allowlist — only sources whose
  state is the API query filter (usaspending) or a poller constant (ca_grants=CA, webs=WA,
  oregonbuys=OR, sam.gov=WA) may tag. Everything else, and any unknown/omitted source, posts
  UNTAGGED. Allowlist not blocklist, so a new source is untrusted until proven.
  (H5) `slot_band()` is now CLAMPED to 04:00–16:30 PT: a hand-typed band of e.g. 17:00–17:30 drew a
  target `in_window` can never admit, silencing the card forever behind two routine-looking log lines.
  STILL OPEN from that review, NOT yet fixed — see the report for detail: (H1) all ~195 SVPP rows
  expire together ~2026-10-05, so only ~54 of them can ever post at 1/day and ~140 expire unsurfaced,
  and the drain order is ~54 near-identical "$500,000 SVPP" cards; (H2) undated `ca-grants-award`
  rows are graded GOLD on ABSENCE of a date, which inverts rule 1 — render `ProjectStartDate` or
  demote to SILVER; (H3) `_short_title` middle-elision still collides when the discriminator is a
  mid-title bid number; (H6) no missed-slot backstop, so a 90-min outage costs the day; (M1) the
  posts-exclusion is global, so a playground post burns a production lead; (M2) no `last_seen`
  staleness filter; (M3) `salesforce_followups` bypasses drip's caps and uses UTC day boundaries.
- `verified` 2026-07-22 C1/C2/H2/H1 from the second architectural-critic review, all fixed:
  **C1 (worst — a false claim in an OUTBOUND EMAIL).** `persequor_client._angle` and
  `slack/persequor.compose_draft` derived wording from `lead_grade`, not the event. When undated CA
  AWARDS were regraded GOLD→SILVER, all ~351 would have been described to a school administrator as
  having "published a solicitation", with the award's SPEND-WINDOW end relabelled a "response
  deadline". Both now derive from `current_event_type` (`rfp_posted` → solicitation;
  `award_*` → award + spend window; `application_window_opened` → opportunity; UNKNOWN → wording that
  claims no award, no solicitation and no deadline). A row lacking the joined event degrades to the
  conservative branch — never a crash, and NEVER an inference from grade. The test that DEFENDED the
  old behavior is replaced by separate silver-award and silver-RFP tests.
  **C2.** `db.channel_guard` is now a PURE READ — an expired guard is filtered out by the query, not
  deleted. It previously self-healed with a DELETE, which crashed `--dry-run` on the read-only
  connection `cmd_drip` opens AND silently wrote during a dry run on a writable one (rule 8).
  **H2.** `delivery_attempts_today` now requires `lead_id IS NOT NULL`. Channel-guard rows share the
  outbox table with a NULL lead_id, and one counted as a delivery — verified to produce `daily cap
  reached (1)` with zero posts and zero reservations.
  **H1.** The permanent block is replaced by a BOUNDED, escalating, channel-scoped guard: 1h→2h→4h,
  capped at 8h, persisting blocked_until / error code / audience / first + latest failure / consecutive
  periods. Reads and dry-runs never mutate or clear it; after expiry exactly ONE attempt is made; a
  confirmed delivery clears it on the writable path; continued systemic failure renews it without
  consuming a lead. `cmd_drip` exits non-zero while blocked, `cli drip-blocked` shows guards
  SEPARATELY from leads (they previously printed as "lead #None"), and one structured
  `[drip][CRITICAL] channel_blocked …` line is emitted per block period.
  NOT claimed: an independent external alert. No MAILTO is set on the droplet, no mail transport has
  been proven, reporting a Slack outage through Slack is not a report, and a keepalive grep is not an
  external alarm. Real alerting is separate, undone work.
  The tautological exit-status test is replaced by one that drives `cmd_drip` with mocked outcomes and
  asserts the actual exit code.
- `verified` 2026-07-22 SIX FURTHER BLOCKERS from Chase's review of `74e8d59`, all fixed:
  (1) A systemic Slack failure now creates a PERSISTENT channel block (`db.set_channel_guard`,
  stored as a NULL-lead_id `notification_outbox` row), releases the lead, returns a non-zero CLI
  status, and stops every later tick until `cli drip-unblock` clears it — previously each 30-min tick
  failed identically and, before the release fix, consumed a lead every time. (2) An UNRECOGNIZED
  HTTP-200 Slack code no longer quarantines: only an explicit `_CONTENT_SLACK_ERRORS` allowlist may
  destroy inventory, because "we don't know what went wrong" is not evidence the lead is unusable.
  Unknown codes RELEASE the lead and report loudly. (3) HTTP 429 is no longer 'unknown' and no longer
  consumes a lead — it releases, reads `Retry-After`, and persists a self-clearing `backoff` guard.
  (4) `--dry-run` now says "WOULD quarantine" and writes nothing, instead of claiming a quarantine
  that never happened. (5) `usaspending-subaward:` and `sam.gov` are REMOVED from the verified-state
  allowlist — their state semantics are `assumed`, never evidenced, and an assumed provenance must
  fail closed. (6) the false comment claiming no constant-state source can post is corrected:
  `ca-grants-portal` reaches production through `bulletin_candidates`.
  New failure tests cover repeated systemic ticks, unknown codes, 429 + lapsed backoff, dry-run
  honesty, CLI exit status, and assumed-source tagging. `db.py` crossed the 1000-line cap and was
  split: `db_delivery.py` now owns reservations, quarantines and channel guards, re-exported from
  `db.py` so every `db.<name>` call site is unchanged.
- `verified` 2026-07-22 SIX BLOCKERS from Chase's review of `85295d7`, all fixed before any push:
  (1) DEFINITIVE Slack failures were classified as ambiguous. `SlackApiError` with HTTP 200
  (`channel_not_found`, `invalid_auth`, `is_archived`, `msg_too_long`…) means Slack ANSWERED and the
  message provably did NOT land — but the blanket handler marked it 'unknown', which after the
  reservation-authoritative change PERMANENTLY consumed the lead. Under a revoked token that silently
  destroyed 1–2 gold leads per weekday while posting nothing. Now split three ways: systemic errors
  (channel/token) RELEASE the reservation and halt loudly with no lead consumed; lead-specific errors
  quarantine as `rejected`; only genuine timeouts/5xx stay `unknown` (a duplicate is worse than a lost
  lead). (2) RENDER failures now quarantine durably via `db.quarantine_lead` instead of crashing the
  tick forever, and `cli drip-blocked` makes every set-aside lead visible — silent loss previously
  looked identical to a quiet week. (3) BOTH candidate exclusions are now audience-scoped, so a
  playground reservation can no longer consume production inventory. (4) `territory` now matches
  constant-state sources EXACTLY (`webs`, `oregonbuys`, `sam.gov`, `ca-grants-portal`) and only
  namespaced ones by prefix — `startswith` would have trusted a future `webs-inferred`.
  (5) UNDATED awards are no longer GOLD (`scoring.py`). GOLD means "just got funding"; granting it on
  the ABSENCE of a date graded on absent evidence and asserted a recency the source cannot support
  (rule 1). This governs the ~347 undated `ca-grants-award` rows — still searchable and exportable as
  SILVER, just not served as proactive GOLD. (6) this file's overclaim, corrected above.
- `verified` 2026-07-22 DEPLOYMENT BASELINE CORRECTION (Chase): production runs `264b0e2`, NOT
  `15263d2` — the 2026-07-22 deploy was verified with a bot restart. So the gold unblock, territory
  tagging and H1/H2 are ALREADY LIVE, and the pending deploy is TWO commits, not six. An earlier
  claim here and in the critic review said otherwise; that was wrong.
- `verified` 2026-07-22 FLOOD BUG found by production-ops-guardian review of `264b0e2` AND FIXED
  (`grant_watch/slack/drip.py`, `db_engagement.py`). `record_post` runs AFTER
  `chat_postMessage`; if it raised (full disk — prod is at 97% — a lock, or a CHECK violation) the
  card was in Slack but `posts` had no row. EVERY cap in `pacing_ok` counted `posts` alone, so the
  next tick read zero and skipped the daily cap, the absolute cap AND the min-gap rule, while
  `mark_surfaced` still excluded the sent lead — so `pick()` returned the NEXT of the 544 and posted
  it, once per 30-min tick until the window closed. Up to 13 cards in an afternoon, each @mentioning
  a rep. FIX: `pacing_ok` now counts `max(posts, notification_outbox reservations)` and takes the gap
  from the latest of either. Reservations are written BEFORE the Slack call, so they cannot be
  missing for a delivered message — the fail-closed signal. Regression test
  `test_cap_holds_when_recording_a_confirmed_send_fails` was PROVEN to fail against the old
  posts-only logic ("cap went blind... eligible") before being confirmed green.
  Same review, two more fixes: (a) `nugget_candidates` now requires `amount > 0` — `_award_facts`
  raises without one and `cli.cmd_drip` has no handler, so an amountless gold lead would crash every
  tick forever, never be surfaced, and stay permanently silent; (b) `urgent` no longer bypasses the
  slot entirely — it may skip the day's random target but not the band OPEN, because it was
  reopening the 04:00 PT front-loading the slot design exists to remove.
  RULED OUT by evidence: the reviewer's deterministic `posts.kind` CHECK trigger. Prod posts 18/19/20
  are `kind='rfp'`, which the pre-migration-13 CHECK (`'nugget','bulletin'`) would have rejected —
  so migration 13's four-kind CHECK is demonstrably live.
- `verified` 2026-07-22 CORRECTION — the 2026-07-21 "probable poller capture bug" claim above the
  gold backlog was WRONG and is retracted. Queried live against the public USASpending API this
  session: **27 of 27** FY25 SVPP (`16.071`) awards across CA/PA/TX/WA return
  `Base Obligation Date = 2025-10-10`, alongside normally-varying amounts and IDs. DOJ obligated the
  entire FY25 SVPP cohort on ONE day. The poller captures it correctly; `distinct=1` is the truth,
  not a defect. Consequence: `PLATINUM_DAYS=7` can essentially only fire once a year, around the
  next cohort obligation (~Oct 2026) — platinum is not a daily tier and should not be treated as one.
  The 347 `ca-grants-award` rows genuinely carry no award date (`event_date=""`, ca_grants.py:211);
  `build_nugget` asserts no date, so they stay honest but rank last (`lead_score` fresh=0.3).
- `verified` 2026-07-22 ROOT CAUSE of "Grant never posts gold" (Chase's report), measured on prod:
  **638 of 638** gold leads had `suppressed=1, backfill=1` — not one exception — so
  `nugget_candidates` returned 0 on EVERY tick and `pick()` fell past platinum and gold to a silver
  RFP daily. Chain: every award poller sets `backfill=True` for anything obligated >90 days ago (or
  merely undated — 427 of 638 have `occurred_on` NULL), `db.upsert_lead:194` turns that into
  `suppressed=1`, and `nugget_candidates` required `suppressed=0`. The flag was a first-rollout
  anti-wave guard that had become a permanent gag. FIXED: `nugget_candidates` no longer filters on
  `suppressed` (the wave it guarded against is already prevented by `DAILY_CAP=1`) and now also
  excludes any lead already in `posts`, so a status reset cannot re-open a posted lead.
- `verified` 2026-07-22 the "same message every morning" was a RENDERING collision, not a repeat.
  Posts 18/19/20 carry three DISTINCT lead ids (PA 07-20 → CA 07-21 → PA 07-22); the dedup fix
  `15263d2` is byte-confirmed live and held. `build_rfp_alert` printed only the agency, a
  regex-derived subject and the deadline — never the title — so prod leads #9533 ("…General and HVAC
  Construction") and #9565 ("…Plumbing Construction *REBID*"), two trade packages of one SCI Pine
  Grove project sharing an agency and a close date, rendered as identical text. FIXED: the card now
  names the solicitation, trimmed at a word boundary.
- `verified` 2026-07-22 territory tagging shipped (`grant_watch/territory.py`): every proactive card
  @-mentions the rep owning that state — PA→Brett D'Ambrosio `U08C1NBH875`, CA→Anthony Dambrosio
  `U01DFJWQQJ3`, WA/TX/OR→Kerry Hilligus `U01E908206M`. All three ids were read from the live Monarch
  Slack directory, never inferred from a name. An unmapped state posts with NO mention rather than a
  guessed one. `GRANT_TERRITORY_OWNERS="PA=U…,CA=U…"` overrides without a deploy; a set-but-malformed
  value yields no tags rather than silently reverting to the built-in reps.
  Chase's original note said "Carrie Hilgus"; no such account exists. He CONFIRMED 2026-07-22 that
  the correct person is **Kerry Hilligus** (`U01E908206M`, kerry@monarchconnected.com) — resolved.
  `grant_watch/presentation.py:state_display_name` now covers all 50 states + DC; drip previously
  knew only 5, so a real Texas award rendered "in TX".
- `verified` 2026-07-21: `python -m pytest tests -q` passed 642 tests (71 skipped live-marked); health
  gate green; `ruff check` clean. The package uses ordered SQLite migrations (through v13), typed
  evidence/funding models, deduplication, scoring (RFPs Silver-at-best, award freshness Gold/Silver),
  guided search with zero-result relaxation hints, per-record verification links, export, Slack
  receipt/reconciliation state, outreach retry state, and Salesforce create-only writes (person +
  organization-only Leads, note-on-existing, fail-closed duplicate guard).
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
