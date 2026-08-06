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

## Current status (2026-08-06)

- `verified` 2026-08-06 **THE RICH CARD HAD NEVER POSTED — NOT ONCE.** Chase reported
  that morning's 11:00 PT card was still "the same incorrect format". It was not a
  regression: efbd8b5 shipped correctly and that card WAS the new layout — but it was
  the FALLBACK (header + sentence + source link), because the rich path has never once
  been eligible. Evidence: `rich_card_snapshots` = **0 rows** in production, and
  `rich-shadow --limit 500` returned **184 candidates, 0 eligible**, every one rejected.
  The single `drip[rich]:` line ever written appeared today: "skip: no rich award card
  satisfies every evidence rule; falling back to the daily card".
  ROOT CAUSE: `GRANT_RICH_CARD_ENABLED=1` was set 2026-08-05 without the jobs that
  populate the evidence its gates read. NONE were in cron: `leads.nces_id` (NULL on
  **176/184** → `entity_kind_unsupported`; only writer is a side effect of one shape of
  Slack search), `leads.org_website` (set on 11/184; only writer is a Slack chat
  conversation), and `salesforce_lookup_state` (**0 rows** — the hard deadlock: with it
  empty EVERY candidate fails `CRM_UNSAFE` regardless of the other two). Compounding it,
  `rich-prepare --limit 25` ranked by raw `lead_score`, and the top 25 contained **zero**
  NCES-bearing leads — the only 8 kind-eligible ones sat at ranks 34–123, so the paid
  Firecrawl/Anthropic batch was enriching leads that could never qualify.
  FIXED (deployed in order): **d66802b** — an untitled contact rendered
  `Contact: Dalton Cagle, — dalton@…`; the comma belongs to the title, and this text is
  the notification/lock-screen surface. **b22ed55** — `nces-bind` CLI (free, keyless,
  preview by default, matching UNCHANGED so an ambiguous name still binds nothing);
  `prepare_worker` targets `preparation.preparable_lead_ids` (skips leads whose first
  rejection cause preparation cannot close — a SPEND fix, not a gate change); org-website
  discovery added, guarded by `_needs_website` so each lead is attempted exactly ONCE
  (`enrich_org_profile` short-circuits only on a prior `found`, so unguarded it would
  re-scrape and re-bill every not_found lead forever). **79db6e1** — `salesforce_sync
  ._candidates` had `LIMIT 500` INSIDE the query, before the sort and with no ORDER BY,
  so the "highest-base-value" ranking only ordered an arbitrary oldest-rowid slice of a
  10,627-lead pool. **03ab7bb** — the redesign that actually closes the deadlock, forced
  by the guardian measuring 79db6e1 against production: a GLOBAL ranking cannot feed a
  TARGETED pipeline. The 8 candidate leads rank **51–165** among 10,627 stale leads and
  `_candidates` hard-caps at 100, so NO `--limit` reaches them (0/8 at 50, 1/8 at 100);
  the old bug had been reaching 5/8 purely by accident. So `prepare_worker` now refreshes
  the CRM snapshot for the leads it is ALREADY preparing, by id
  (`salesforce_sync.refresh_lead` + `_crm_is_stale`), which also removes the need for any
  `salesforce-sync` cron line — `rich-prepare` at 07:45 PT now completes the whole chain
  (contact → website → CRM → activity). Also in 03ab7bb: `salesforce-sync --dry-run` was
  **NOT dry** — it called `lookup()` per candidate and skipped only the local write,
  spending 150–350 live production API calls; a preview now makes no request at all.
  NO EVIDENCE RULE WAS RELAXED in any of these — Chase's instruction was to leave the
  gates alone, and the fixes only make the evidence exist and aim the paid work.
  `pytest` 1001 passed / 74 skipped; ruff + format clean. Every new test proven
  load-bearing by mutation — including one that was NOT: the fresh-CRM-snapshot test
  initially passed against broken code because `prepare_worker`'s per-candidate
  `except Exception` swallowed a raising stub. Rewritten to RECORD calls rather than
  raise. A test that looks strict and asserts nothing is exactly what rule 3 exists for.
- `needs-testing` 2026-08-06: **no rich card has ever been submitted to Slack's live
  Block Kit validator.** `card.render`, the `rich_not_relevant` button binding, and the
  snapshot round-trip are all unproven against real Slack. The first live post IS the
  first real test.
- `verified` 2026-08-06 lead #1603 (Hoxie School District No 46, AR, $500k SVPP) became
  the first ever ELIGIBLE rich card, at `card_mode=research_needed`. Its website
  provenance is `verified_org_page` (a scrape), and draft-ready requires
  `nces`/`authoritative_directory` — but `leads.nces_website` has NO writer anywhere in
  the codebase, so **no lead can currently reach draft-ready** and no card carries the
  "Ask Persequor to draft" button. Wiring `nces_website` from the record `nces-bind`
  already fetches is the change that would unlock it; deliberately NOT done, because that
  button is the outreach path.
  Its NCES binding needed an operator-supplied LEAID: USAspending reports the legal name
  `HOXIE SCHOOL DISTRICT NO 46`, NCES `LEA_NAME` is `HOXIE SCHOOL DISTRICT`, and
  `normalize_name` strips "school"/"district" but not "no 46" — so `hoxie no 46` ≠
  `hoxie`. `nces.normalize_name` was deliberately NOT loosened: relaxing an exact matcher
  is how a lead binds to the WRONG district, and a wrong `nces_id` freezes into a card
  snapshot. That needs a cross-state false-positive audit first. Identity was confirmed
  from three independent sources (single AR Hoxie district; USAspending recipient address
  602 SW HARTIGAN ST, HOXIE 72433; the scraped site's own address matching).
- `assumed` 2026-08-06 NCES coverage ceiling, measured live across the 5 most-pending
  states: of 69 pending leads only **19 can ever bind — 50 (72%) never will**
  (`AJO UNIFIED SCHOOL DISTRICT 15`, `SCHOOL DIST 103`, `DEKALB … #428`,
  `FRANKFORT INDEPENDENT BOARD OF EDUCATION`). Because `nces-bind` orders states by
  pending DESC, permanently-unmatchable states stay pinned at the top forever and the
  other 31 states are never reached — which is why the cron line is WEEKLY with
  `--limit-states 12` (sweeps all 36 in three Mondays) rather than daily. The real fix is
  marking unmatchable leads so they stop re-queuing; NOT done.
- `needs-testing` 2026-08-06 two known silent-failure paths, neither fixed:
  (1) `CRM_FRESH_HOURS = 24` with a once-daily refresh means ONE missed `rich-prepare`
  guarantees staleness at the next day's window — every affected lead flips to
  `CRM_UNSAFE`, the rich path misses, and it falls back SILENTLY (one `drip[rich]:` line;
  no MAILTO is set on the droplet). Widening to ~36h would give real slack.
  (2) `cron.log` is 1.7 MB with no rotation.
- `verified` 2026-08-06 OPERATIONAL LESSONS worth keeping: (a) a long ssh one-liner
  WRAPPED in the terminal between `.venv/bin/python` and its script argument, and that
  newline inside the single quotes split it into two commands — a bare `python`
  (interactive REPL, hung on stdin) plus an orphan line. The repost silently never ran;
  Ctrl-C landed before `reserve_notification`, so nothing was left to clean up
  (`integrity_check` ok, zero `rich_award` outbox rows, `proactive_daily_slots` empty).
  Keep operator commands SHORT. (b) The guardian twice caught its own bad verification —
  an artifact proof that printed PASS while comparing ZERO files (`git hash-object` run
  with cwd outside the repo), and a discriminator that string-matched `LIMIT 500` in
  source that now merely QUOTES it in a comment. Behavioral checks beat textual ones.
  (c) A `__pycache__` purge destroyed the `.pyc` mtime evidence of an incident under
  investigation. (d) The CPU spike Chase reported during the failed repost was NEVER
  explained: the guardian's own `rsync -cain` audit was the leading suspect and it
  instrumented that away (0.457 s, load average unmoved). Left honestly unexplained.
- `verified` 2026-08-06 00:15 PT PRODUCTION DEPLOY 359c1e3 → **5f09200** (guardian,
  scoped SSH, sanctioned git-archive + checksum rsync; 46-file delta matched exactly,
  0 deletions, `__pycache__` purged, revision stamp updated, crontab/`.env` shas
  unchanged, listener PID 633555 untouched — no restart needed, `verified` by
  importing the bot's full lazy closure against the semantic-change set). Ships the
  rich→daily fallback + Block Kit restyle (efbd8b5) and the no-routing-line revision
  (5f09200). Post-deploy `drip --dry-run` at 00:15 PT: "skip: waiting for today's
  10:41 Pacific slot" — the RICH path is live. Two stated deploy assumptions were
  CORRECTED by the guardian with evidence: migrations_rich.py byte-differs
  (ruff rewrap) but is AST-identical — schema stays 28; and `territory` IS lazily
  bot-reachable but no bot-side module calls it, so no restart was required.
  SEED EVIDENCE (read-only ledger counts; the manual run's stdout was lost with its
  session): 25 paid contact-refresh attempts 05:49–05:55Z on 08-05 — 22 completed,
  3 indeterminate; `contact_evidence` = 22 rows: **7 verified, 15 not_found**;
  0 salesforce_activity_snapshots. `needs-testing`: TODAY'S first live card
  (~11:00 PT tick for the 10:41 slot) — whether rich or fallback, a human must
  confirm it renders and the rep mention notifies (critic H2); nothing has yet
  proven `render_blocks` or the rich card against Slack's live validator. Droplet
  disk 64% used / 18G free (~9G freed since 07-25, not by this session).
- `verified` 2026-08-05 RICH CARD ENABLED IN PRODUCTION on Chase's explicit instruction
  ("No just make it live"), WAIVING his own A4 five-business-day shadow gate after the
  tradeoffs were explained. Through grants-ops-guardian over the scoped grants SSH only:
  `GRANT_RICH_CARD_ENABLED=1` appended to the tenant `.env` (sha `fe9fd588…3f55` →
  `b3f338ff…c3bff`, exactly one line added) and ONE cron line added — `45 7 * * 1-5
  … rich-prepare --execute` (paid Firecrawl contact discovery + read-only Salesforce,
  bounded --limit 25) — original 4 cron lines byte-identical (sha `6275d502…44711` →
  `70e309aa…876f`). Preflight held: prod `359c1e3`, schema 28 read-only, one healthy
  listener, no bot restart needed (nothing in the bot process reads the flag; card
  buttons bind to frozen snapshots). Prepare preview: 25 candidates. `needs-testing`:
  the paid seed run's counts and the post-enable `drip --dry-run` (guardian session was
  still executing them at last report); the first LIVE rich/restyled card render and
  its rep phone notification (critic H2 — a human must confirm tomorrow's 10:00–11:30
  PT card); AZ and 44 other states + DC remain UNMAPPED in territory.py — and per
  Chase's SAME-DAY revision, an unmapped state now renders NO routing line at all: no
  tag AND no "unassigned territory" label (that label was his 2026-07-22 choice; he
  dropped it 2026-08-05). Applied to territory.routing_line, the rich card render
  (route section omitted), and the rich fallback text; owners are still never guessed
  and inferred states still cannot tag.
- `verified` 2026-08-05 NEW-LOOK-EVERY-DAY fallback (Chase chose it over silent days
  and over old-card fallback): with the flag on, a rich tick that provably cannot post
  today — `delivery.fallback_to_daily`: eligibility miss, candidate-changed,
  stable-delivery-exists, already-reserved, or the rich cutoff passed — hands the tick
  to the legacy drip, whose card is now RESTYLED into the rich Block Kit layout
  (`grant_watch/slack/drip_card.py`): header (kind label), the builder's exact
  sentence, the territory routing line, and the source link as blocks; `text` stays
  the full proven string, and a test enforces every block is a substring of it (no new
  claims; no contact/CRM/website/buttons on this path). Cap/guard/ambiguous/waiting
  outcomes NEVER fall through — architectural-critic review found NO double-post
  sequence (the cap counts posts + pre-Slack reservations on both paths) and no
  unclassified outcome; its H1 (string drift) closed via shared constants at the
  return sites. Rich hard cutoff moved 11:00 → 11:30 PT (`pacing.HARD_CUTOFF_PT`):
  slot minutes 10:31–10:45 were UNREACHABLE on a :00/:30 cron grid because the first
  tick after the slot was refused — ~a third of rich days would have silently fallen
  back. After an outage past the cutoff the fallback card may land in the afternoon —
  accepted, per "never a silent day". Also fixed: three PRE-EXISTING bulletin-test
  failures from fixture date-rot ("open through 2026-08-04" expired 2026-08-05; pinned
  to 2030 per the file's convention). `pytest` 992 passed / 74 skipped; ruff + format
  + vulture + health clean. OPEN from the critic, NOT fixed: (M1) salesforce_followups
  arbitrates only via slot rows which legacy `pacing_ok` cannot see — MUST be fixed
  before any followups cron is ever scheduled (none exists today); (M2) a crash between
  slot-reserve and outbox-reserve leaves an orphan slot row = one silent day, no
  sweeper; (M5) `--dry-run` can mispredict WHICH path posts (rich preview returns
  before the veto/prior checks).

- `verified` 2026-07-25 PRODUCTION DEPLOY of the Salesforce Campaign-member fix.
  Production moved `e8ecf0c` → **`359c1e3`**, schema **26 → 28**, through
  grants-ops-guardian over the scoped grants SSH only. `integrity_check` ok. Migration 27
  adds the five `crm_campaign_*` ledger tables (batches / batch_targets / batch_items /
  write_attempts / approval_attempts) plus `crm_actions.batch_id`/`batch_target_id`; all
  five are present and EMPTY. Migration 28 enforces one ready Campaign-creation preview
  per `(workspace, channel, thread_ts, requested_by)` via unique partial index
  `ux_crm_one_ready_campaign_creation`, keeping the NEWEST by `(created_at, rowid)`.
  Exactly the two Chase-approved older duplicates were cancelled (`1de9fac0…`,
  `ef622493…`, both `last_error='Superseded by migration 28: duplicate ready Campaign
  preview'`); `b620bd04…` and the unrelated `6f90999e…` stayed ready; all four kept
  `external_write_started=0`. A full sweep of all 45 `crm_actions` rows against the
  pre-migration backup found EXACTLY two rows changed in EXACTLY three columns
  (`state`, `last_error`, `updated_at`) — the kept rows are byte-identical.
  **MIGRATION 28 MUTATES DATA**, so its rollback is restore-from-backup, not a reverse
  migration; there is also NO migration CLI and no preview — `db.connect()` on a writable
  connection IS the migration (`connect_readonly()` deliberately does not migrate).
  Rollback artifact retained: `backups/deploy-359c1e3-20260726T012742Z/grant_watch.db.pre28`
  (`VACUUM INTO`, sha256 `79a918db…76ef`, verified ok/26/2FK/4ready) + `code_before.tar.gz`
  (`d974adbc…23ae`), both re-checksummed after cleanup. A code rollback MUST also delete
  the DB's `-wal`/`-shm` before restoring, `rm` the 15 files `359c1e3` adds (tar cannot
  delete them), and purge `__pycache__` — otherwise the restarting bot's `db.connect()`
  silently re-applies 27/28. Cron restored byte-for-byte (4 lines, sha `6275d502…44711`);
  `.env` sha `fe9fd588…3f55` unchanged; one listener PID 633555, correct uid/cwd/argv,
  "Grant is listening" + Bolt running, PID stable. NO production Salesforce call and NO
  Slack post occurred during the deploy (the migration path imports only `sqlite3`; the
  bot was down throughout). The TWO PRE-EXISTING `source_observations` FK violations
  (rowids 10642, 11892) were PRESERVED unchanged on Chase's explicit approval — the same
  2026-07-21 decision recorded below; `foreign_key_check` returns exactly those two and
  no new ones. **`SALESFORCE_CAMPAIGN_WRITES_ENABLED=1`** — production Campaign writes are
  ARMED (still gated per record by `verify_write_scope` + a requester-bound Slack button);
  it was NOT changed by this deploy and no production Campaign write has fired.
  Outage was **25.5 minutes** (18:46:50 → 19:12:19 PT), not the ~15 estimated — the
  5.5-min keepalive drain plus per-step verification is the gap; budget 30.
  HONEST NOTES: (a) the guardian's own post-migration checker crashed with
  `KeyError: 'batch_id'` because it compared `select *` against a backup lacking a column
  migration 27 ADDS — the migration was fine, the CHECK was broken; rolling back there
  would have destroyed a good migration over a tooling bug. (b) Two earlier Phase-B
  attempts were halted by the Claude Code permission classifier; the guardian stopped both
  times rather than reshaping the command, and production was verified byte-for-byte
  unchanged each time. (c) `deploy_rsync.sh` must NOT be used — both copies push from
  Chase's LAPTOP working tree; the sanctioned mechanism is a pinned `git archive` artifact
  (sha256 `a529250e…92099`, proven `diff -r`-identical to `359c1e3`, 895 files, 0 symlinks,
  no `.env`/`.git`/db/secrets) plus droplet-local checksum rsync (34 transferred, 0
  deletions, protected-path audit PASS).
- `verified` 2026-07-25 the registered Slack Campaign button handlers now have refusal-path
  coverage (`tests/test_salesforce_slack_action_paths.py`, commit `6848293`). No test
  previously drove the registered Bolt `salesforce_confirm`/`salesforce_cancel` callbacks
  end to end, so the audit → terminalize → reply wiring was unproven for malformed payload,
  unconfigured channel, inactive/non-member actor, EXPIRED preview, wrong approver, and
  cancel-by-stranger. Each test drives the actual callback and asserts no Salesforce HTTP
  via a `requests` stub that raises. All six were PROVEN load-bearing by mutation: removing
  the expiry guard, the configured-channel gate, and the active-member gate failed exactly
  the three matching tests, and the mutated actions reached state `failed` — i.e. they
  ATTEMPTED the write path and were stopped only by the no-network stub. `pytest` 977
  passed / 71→74 skipped; ruff format now clean repo-wide (159 files).
- `needs-testing` 2026-07-25: NOTHING in the Campaign-member fix has been exercised against
  live Slack or PRODUCTION Salesforce. The five ledger tables are empty. The surviving ready
  action `b620bd04…` ("New Jersey Grant 2026", production channel `C01DGT9D11D`) is past
  `expires_at`, so `confirm_action` → `_authorize_action(require_ready=True)` should raise
  `TimeoutError`, mark it EXPIRED and refuse before `_begin_commit` — `assumed` from source
  and the new tests, NOT observed live. The first real click is still the first real test.
  Also open: ~1.5 GB of tenant cruft the guardian found and deliberately did not touch
  (28 `.grants_agent.previous.pre-*` trees ≈1.1 GB, 17 db backups ≈198 MB, 12 stale
  `.deploy_staging/*` dirs ≈274 MB) — Phase D, needs per-path approval; 22 G free so not urgent.
- `verified` 2026-07-23 RICH-CARD GATE LOOSENING (Chase approved Changes 1 & 2 with
  revisions + a narrowed Change 3 after the 14-candidate audit). Local only, flag OFF
  (`GRANT_RICH_CARD_ENABLED` default false), NO deploy/enable/prod-write/live-post.
  Migration 25 freezes typed provenance (`rich_card_snapshot_truth.official_website_
  provenance` + `contact_domain_binding`, `rich_card_snapshots.card_mode`). See design
  §16. **Change 1**: contact email binds to the ORGANIZATION, not the scrape page
  (`policy.contact_binding` → `org_site` when the email domain matches the verified org
  website incl. a parent/subdomain, or `authoritative_directory` when verbatim in an
  EXACT, id-bound record on a human-reviewed host allowlist `REVIEWED_DIRECTORY_HOSTS`
  = nces.ed.gov, cde.ca.gov). Exact binding only — nces.ed.gov needs the lead's nces_id
  in the URL; cde.ca.gov cannot exact-bind (no stored CDS code) and stays rejected. No
  suffix heuristics. **Change 2**: typed `policy.website_provenance` (`nces` /
  `authoritative_directory` / `verified_org_page` / `none`), frozen with its evidence
  locator; a reviewed-directory host is NEVER an org's own site (the Fairfax safety);
  `verified_org_page` fires from an org-site scrape OR a verbatim-verified contact on
  that domain (the Bartlett fix). `nces`/`authoritative_directory` are modelled +
  fixture-tested but INERT until that source is wired — not claimed live. **Change 3
  (narrowed)**: exact/complete-no-match stay draft-ready; fresh `ambiguous` → a
  `research_needed` card ("Possible Salesforce matches—review before outreach", NO
  relationship/net-new claim, TERRITORY routing only with every CRM binding dropped so a
  single-account/multi-opp ambiguity can't leak an owner, NO active Persequor button, and
  `actions.request_draft` refuses it server-side); partial/unavailable/stale/missing
  remain ineligible. Event wording exact ("Federal funds obligated"/"Award announced",
  never "Awarded"); double-period fixed; audit output redacts email local parts; removed
  dead `card.exact_award_url`/`official_site_evidenced`. Non-negotiables held: award
  truth, personal-mailbox rejection (regression-tested), contact freshness, Persequor
  safety. `python -m pytest tests -q` = 919 passed / 71 skipped; ruff + vulture + health
  clean; largest touched file 485 lines.
- `verified` 2026-07-23 RERUN of the same 14 on a DISPOSABLE copy of prod_preview.db with
  REAL authorized enrichment (Firecrawl + Claude + read-only Salesforce), both audiences,
  emails redacted. Under the new policy: PRODUCTION 5 eligible (was 0) — 1 draft-ready
  (#7789 Bartlett ISD, no-match CRM, website via verified contact page) + 4 research-needed
  (#231 Birmingham, #232 Montebello, #235 Valle Lindo, #241 Golden Eagle; all ambiguous
  CRM, all territory-routed). PLAYGROUND 4 eligible (#231 & #7782 excluded — already in
  that audience's post history). The 3 originally-over-narrow leads (235 org-site subdomain
  bind, 241 org-site, 7789 contact-establishes-site) are now eligible — Changes 1 & 2
  validated on live data. SAFETY validated: #234 Fairfax REJECTED website_provenance_missing
  because its "website" is the cde.ca.gov directory, not its own site. Remaining rejects
  are genuine: 7 contact_missing (no verifiable contact) + 2 website_provenance_missing.
  `assumed` replenishment: ~5 eligible now ≈ one business week at 1 card/weekday; near-term
  replenishment is LOW without enriching more of the 563 gold leads (most lack an NCES id
  or a verifiable contact) — the 14 were a pre-characterized NCES+dated+open-window subset,
  not a rate. NOTE: my first two rerun attempts were HARNESS artifacts, both caught and
  corrected — (1) `.env` never loaded at all (false all-website_missing). **CORRECTED
  2026-08-06: this said "not loaded from the background cwd", which blamed the wrong
  thing and cost a wasted production run today.** `load_dotenv()` is called only inside
  the three entrypoints' `main()` (`cli.py`, `slack/grant.py`, `source_discovery_batch.py`);
  importing `grant_watch.*` triggers none of them. cwd only matters GIVEN such a call.
  Any script that imports the package directly must call `load_dotenv()` itself;
  (2) `now` captured before enrichment, so SF `checked_at` read as a future timestamp and
  the freshness guard correctly rejected it (false all-CRM_UNSAFE). The policy was right
  both times; re-evaluating with a correct clock produced the numbers above. Frame this as
  a **production-audience simulation on a disposable production-data copy**, NOT production
  output.
- `verified` 2026-07-23 architectural-critic READ-ONLY review of the complete uncommitted
  diff (Chase-ordered, ten focus areas). Verdict: 9/10 properties hold against running
  code — ambiguous-CRM owner-drop is airtight (nulled before routing), the research-card
  draft refusal is server-side on the frozen `card_mode`, migration 25 is forward-only /
  nullable / rollback-inert with matching freeze() placeholder counts, complete-no-match is
  freshness-gated, feature-OFF dispatch is unchanged, and the report is counts-only (no PII).
  Two findings resolved before commit: **H1** — `_authoritative_exact` matched the NCES id
  by SUBSTRING (`in`), so `062271` could bind another district's `?ID=0622710`; now matched
  as a whole query value / path segment (was INERT — no runtime source feeds that path —
  so never Critical, but fixed + adversarially tested). **M1** — `_same_site` now requires a
  dotted label on both sides so a bare public suffix (`net`) cannot bind. **H2 (honesty,
  not a code bug)** — the "non-heuristic provenance" claim OVERSTATED: the website-to-awardee
  tie still rests on `finder._looks_official` (a name-token anchor); corrected the claim in
  `policy.website_provenance`'s docstring, design §16, and here — it is non-heuristic AT THE
  POLICY LAYER given that anchor, not end-to-end. RESIDUAL, documented not closed: a
  multi-label public suffix (`k12.ca.us`) can still `_same_site`-match, but a real contact
  email at a bare public suffix must still pass finder's on-page verification (critic-rated
  Low); full PSL is out of scope. All 4 simulated research cards routed to a mapped
  territory rep (none unassigned). `pytest` 925 passed / 71 skipped; ruff + health clean.

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
- `verified` 2026-07-22 C-1/C-2/H-1 from the FOURTH critic review. **C-2 first, because it is about
  this file's own honesty:** the record-semantics commit claimed as `verified` that "the draft a human
  approves can no longer disagree with the brief" — in the very entry that corrected a previous false
  `verified`. THAT CLAIM WAS FALSE. The real flow (`slack/grant.py:688`) is `build_brief` →
  `submit_brief` → **the POST happens** → `compose_draft` renders ONLY as fallback copy when
  submission failed. The rep approves a yes/no question ("Want me to have Persequor draft the intro
  email?"); on the success path they never see the draft, and they never see the brief's fields at
  all. Corrected in CLAUDE.md, `compose_draft`'s docstring, `RecordSemantics`' docstring and
  `build_brief`'s comment. The true guarantee is narrower: the fallback draft and the payload derive
  from one object, so the two DESCRIPTIONS cannot diverge. Whether a rep should see the asserted facts
  before the POST is an open product question, not a fixed bug.
  **C-1 (a fabricated award claim in outbound email).** `amount_usd` shipped UNCONDITIONALLY, ungated
  by record kind. Every prose surface correctly refused to say money was awarded, then the payload
  handed Persequor — an LLM drafting agent — `program='SVPP'` + `amount_usd=487657`, which IS an award
  claim however hedged `angle` is. The create-only Salesforce headline had the same hole
  (`SVPP · $487,657`). FIXED via a new `asserts_amount` facet, gating both. `window_meaning` was
  REMOVED again: `outreach-request.v1` is a pinned EXTERNAL contract, an unknown key would 422 every
  brief if Persequor forbids extras, and that is unasked. A test now pins the exact serialized key set.
  `verified` 2026-07-22 by grants-ops-guardian, read-only: `OUTREACH_TEST_EMAIL` IS set to a non-empty
  value in BOTH `.env` and the live process environ (PID 1859872), no drift — so C-1 could not have
  reached a school administrator. The value was never printed. Name-presence alone had NOT proved this
  and was explicitly not treated as proof.
  **H-1 (the incident reset was defeated on tick 2).** `set_channel_guard`'s upsert did `attempts+1`
  and never reset `created_at`, silently undoing the caller's fresh count — so escalation jumped to
  the 8h cap and `first_failure` reported a months-old date. FIXED atomically IN PERSISTENCE
  (`reset=True` deletes the row inside the same transaction). `_incident_lapsed` now measures from the
  guard's EXPIRY, not its last write — measuring from `updated_at` made every incident lapse at each
  8-hour boundary, so the ladder ran 1h→8h→1h→8h and never held.
  Also: an active BLOCK now outranks a shorter backoff for reporting and exit status (a 429 picked up
  during `drip --force` could mask a live credential outage behind a benign exit-0 line);
  `drip-unblock` no longer discards an active rate limit.
  `needs-testing`: five duplicate event-type mappings REMAIN (`grade_phrases`, `_record_clause` SQL,
  the drip builders, the three candidate queries, and a SECOND `RecordKind` enum in `search.py`). The
  critic advised NOT routing the drip builders through the helper — their fail-closed gating is
  stronger than its permissive fallback — so the fix is sharing constants, not the function.
  `needs-testing`: `grade_phrases` infers record kind from a LIMIT-ed 50-row slice and applies it to
  full-set counts, so a 500-row mixed result can be headlined "award won". Last grade→meaning leak.
- `verified` 2026-07-22 THE ARCHITECTURAL FIX Chase ordered after the third review: record meaning was
  derived INDEPENDENTLY in five places, two of them from `lead_grade`. Patching copies was producing
  one new defect per round. Now there is ONE typed helper — `grant_watch/record_semantics.py` —
  holding `RecordKind` + a frozen `RecordSemantics` per kind, derived ONLY from `current_event_type`.
  **The rule: GRADE decides PRIORITY; EVENT TYPE decides WHAT HAPPENED. Never the reverse.**
  All five consumers route through it: search/export `window_label` + `entity_role_for_row` +
  `_record_kind_for_row`; the Persequor preview AND payload (from the SAME object, so the draft a
  FALLBACK DRAFT can no longer disagree with the brief that writes the email — `build_brief` omits
  window dates entirely when the kind cannot give them a meaning); the model's own instructions in `conversation.py`/`tools.py`, which had been TEACHING the
  false grade→date equation; and the permanent create-only Salesforce headline + note, whose lead row
  is now JOINED (`db.get_lead`) rather than a bare `SELECT * FROM leads` that silently degraded every
  real award. `record_observed` (migration 6's backfill shape) asserts nothing at all.
  Also fixed this round: `cli drip-blocked` crashed on any guard (C-1, above); the conservative draft
  branch LEAKED raw source keys (`seed:svpp_csv`) into outbound email; systemic blocks and 429
  backoffs now use SEPARATE guard rows so a 30-second rate limit cannot overwrite an 8-hour block or
  inflate its counter; a lapsed incident resets the escalation instead of inheriting a months-old
  count; `first_failure` is the actual failure time, not the future unblock time; quarantines return
  `quarantined:` (non-zero exit) with a structured `[drip][CRITICAL] lead_quarantined …` line instead
  of reading as a routine `skip:`; stale comments describing the deleted operator-cleared semantics
  are gone.
  Cross-consumer truth tests in `tests/test_record_semantics.py` were PROVEN to fail against the
  grade-driven behavior (4 failures) before being accepted, and the `drip-blocked` test was proven to
  fail against the old projection. THREE test fixtures were found building "awards" with the default
  `record_observed` event — the same fixture-realism defect that let the RFP-title bug ship — and now
  set an explicit event type.
  `needs-testing`: how many PRODUCTION rows land in the unknown branch. All 403 local leads carry
  `record_observed`; if prod is similar, drafts for real awards became materially vaguer and the
  remedy is an evidence-based backfill, NOT looser wording. Gated behind Chase's read-only production
  aggregate — do not infer prod from the local DB, and do not backfill without that evidence.
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
  consuming a lead. `cmd_drip` exits non-zero while blocked, and one structured
  `[drip][CRITICAL] channel_blocked …` line is emitted per block period.
  **CORRECTION (Chase, 2026-07-22):** the original version of this entry claimed as `verified` that
  `cli drip-blocked` "shows guards SEPARATELY from leads". It did not — it CRASHED with `IndexError`
  whenever a guard existed, because `available_at` was missing from the projection while the renderer
  printed it. That claim was never executed. Labelling unrun behavior `verified` is exactly the
  failure rule 1 exists to prevent, and it was in the file that is supposed to be the honest record.
  Fixed and now genuinely `verified` by a test proven to fail against the old projection.
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
- `verified` offline 2026-07-22 on `review/rich-award-card-campaign-20260723`: the rich
  award-card layer is implemented behind `GRANT_RICH_CARD_ENABLED=0` with migrations
  14–24, strict evidence policy, durable possibly-paid preparation, exact owner routing,
  versioned immutable snapshots, precision-safe Block Kit, one-card pacing,
  reservation-before-Slack, snapshot-bound Persequor/feedback actions, and frozen-only
  thread evidence. No live Slack,
  Salesforce, paid enrichment, Persequor, or production operation proves this feature.
  Production migration/volume/layout and the five-business-day shadow remain
  `needs-testing` and separately authorized through `grants-ops-guardian`.
- `needs-testing`: a positive OregonBuys/WEBS security row, Salesforce sandbox Campaign
  creation/membership, Salesforce production writes, Postgres parity, and the drip-thread reply path
  from a genuine phone client. Salesforce Campaign writes stay disabled until explicit sandbox
  approval; all sandbox test records await Chase's delete/keep decision (Ben Bayle, Wally Rakestraw,
  Richard Moline, ZZ FLS Probe).
- `assumed` next sequence: decide the gold-backlog surfacing product question, characterize
  high-value catalog candidates one source per module with fixtures and live smoke checks, then keep
  operating the droplet only through `grants-ops-guardian`.
