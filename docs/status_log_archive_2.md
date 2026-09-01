# Grant status log — archive II (2026-08-09, the first live-tested day)

Split from `docs/status_log.md` on 2026-09-01. The rotation chain CLAUDE.md
describes — current state in CLAUDE.md, older entries in `status_log.md`, oldest
in `status_log_archive.md` — had run out of room: the archive was at 795 lines
and this block is 218, so the usual oldest-first move would have broken the
1000-line cap it exists to respect. A second archive file is the same split the
other two already are.

Nothing is retired. Several entries here correct an earlier claim that turned out
to be false, which is the history most worth keeping.

## Current status (2026-08-09, live-tested)

- `verified` 2026-08-09 SIX PLAYGROUND THREADS driven as a real user, deliberately messy.
  Nebraska (search → gold filter → campaign preview with working Confirm buttons → delete
  refusal → ZoomInfo free preview + approved 2-record paid pull → LinkedIn misattribution
  refusal → supplied phone recorded → campaign status). Michigan (*"hey do we have
  anything good in michigan"*; *"whats the story with bellaire? are they a small
  district"* → ~260 students, $1,900/student; *"yeah check salesforce for bellaire"* →
  two matches flagged POSSIBLE not certain, health-centre one called out as probably
  unrelated, working links, deferred to the existing owner). Texas (*"can u put together
  a spreadsheet of all the open rfps"* → honest zero-result with real alternatives;
  *"just email me the 29 texas ones"* → **Grant CORRECTED ITS OWN EARLIER NUMBER**
  unprompted — "the real count is 516, not the 29 I mentioned earlier" — said plainly it
  cannot send email, and attached `grant_search.xlsx` (73 KB) instead. That is the exact
  dead-end that lost Kerry on 2026-07-24.) Ambiguity (*"go enrich this"* with no context
  → ONE short question, "which lead do you mean?"; *"the bellaire one in michigan"* →
  resolved, enriched, honest not_found, and volunteered the Salesforce ownership
  conflict). And asked whether the Nebraska campaign existed → correctly said it did NOT
  find it and asked for the exact name, rather than claiming a campaign whose Confirm
  button was never clicked.
- `verified` 2026-08-09 THE ATTRIBUTION SURFACES TO THE USER BY ITSELF: asked to recap,
  Grant said the Scottsbluff phone is "David Davis's direct line, which you gave me and I
  logged as supplied by you". That sentence is the whole point of `human_asserted`.
- `verified` 2026-08-09 SALESFORCE CHECKED IN THE BROWSER, not just the API: "California
  Grant 2026" opened in Lightning showing real Lead members (Birmingham Community
  Charter, Montebello Unified, San Ysidro, Fairfax, Valle Lindo, Galt Joint Union).
  ZoomInfo's own web app renders blank in that browser context, so its DATA was verified
  against an INDEPENDENT source instead — Scottsbluff's own site lists "David Davis,
  Director of Information Technology", corroborated by LinkedIn and a local newspaper.
  Checking ZoomInfo's UI would only have been the same source twice.
- `verified` 2026-08-09 **THE FOLLOW-UP WORKER COULD NOT WORK ITS OWN BACKLOG.** With a
  2-day grace and a 5-day drop window the eligible slice was only THREE days wide, so
  everything that accumulated while the feature was off aged out before the feature could
  see it — 28 of 36 due subjects were already unreachable on the day it shipped, and all
  18 PLAYGROUND subjects with them. `DROP_AFTER` widened to 14 days. A test pins the other
  end so the worker does not become an archaeologist.
- `verified` 2026-08-09 MY MESSAGES CANNOT WAKE GRANT WITHOUT AN @-MENTION, and that is a
  harness artifact, NOT a product defect: messages sent through the Claude Slack app carry
  an `app_id`, and `grant.py:295` deliberately ignores those (the guard that stops
  bot-to-bot loops — the same guard that limited the damage from Monarch_Sales_Agent). A
  human typing in a thread has no `app_id`, so plain replies work for real reps. Proven by
  reading the raw Slack payload and by a plain reply from me being correctly ignored.

## Current status (2026-08-09, final)

- `verified` 2026-08-09 **CHASE'S "RIGID, PROGRAMMATIC" FAILURE IS FIXED AND PROVEN LIVE.**
  Grant refused a phone number a rep typed into chat because it had not come from a
  source Grant pulled. That was the honesty rule pointed at the wrong case — it exists
  to stop Grant INVENTING a contact and calling it discovered, never to stop a person
  telling Grant something true. The rule is now ATTRIBUTION, not refusal: a supplied
  fact is stored as its own `human_asserted` row with who said it and when.
  Live in production: *"ok david davis direct line is 308-555-0142, add it to that
  scottsbluff lead"* → *"Done — saved on the Scottsbluff Public School lead, logged as
  supplied by you today."* No lead id, no structure, "that scottsbluff lead" resolved
  from thread context. Messy input also proven: *"whos the tech drector at medicine
  valley"* (two typos, partial name) returned Scott Trimble, Superintendent,
  page-verified with email and phone, plus an honest "no dedicated Technology Director
  listed".
- `verified` 2026-08-09 **THE FIRST VERSION OF THAT FIX WAS WORSE THAN THE REFUSAL**, and
  the architectural-critic EXECUTED the exploit rather than describing it. Filling empty
  fields on the contact the rep named left the row still reading
  `contact_status='verified'` while carrying a value nobody checked — and `grant.py`
  selects the Persequor outreach brief's contact on exactly that status, so a typed
  number would have been EMAILED TO A SCHOOL ADMINISTRATOR as Grant's own verified
  finding. The trigger is the likely case, not an edge one: a staff directory with a
  name, title and phone but no email is common. Fixed by always inserting a separate
  row. Twelve further findings fixed in the same pass, including: `provenance` was
  populated by a ONE-SHOT backfill so every contact created afterwards had it NULL and
  the guard protecting page-verified evidence was blind; the confirmation was built
  from the ARGUMENTS so a dropped field was still reported as recorded; and the removal
  refusal was disarmed by any additive word anywhere ("delete that campaign AND add the
  new one"). All mutation-proven.
- `verified` 2026-08-09 SELF-CAUGHT BY LIVE TESTING: the new campaign-status tool
  reported **"0 members"** for a Campaign holding 13, because a bare `SELECT COUNT()`
  returns its total in `totalSize` and ZERO rows. Grant then reasoned confidently on top
  of the false zero and told a rep someone must have removed them. The unit test MASKED
  it by stubbing thirteen empty dicts. `COUNT(Id)` fixed it; a second test pins the
  query text, because the two implementations are indistinguishable from output alone.
  The false claim was corrected in the Slack thread.
- `verified` 2026-08-09 A TEST WROTE TO THE DEVELOPER'S OWN DATABASE. `db.connect` binds
  its default AT IMPORT TIME, so monkeypatching `db.DEFAULT_DB_PATH` does nothing and a
  bare `db.connect()` kept opening the real file — migrating it and leaving rows. Rows
  removed; an autouse conftest guard now FAILS any test that opens the real database via
  `connect` or `connect_readonly`, and it immediately caught three more.
- `verified` 2026-08-09 PRODUCTION IS ON `2239a18` AT SCHEMA 32 after five staged
  deploys today (`90f0420`→`fe56807`→`3cf9df0`→`2239a18`), outages of ~4s, ~2s and ~1s.
  Migration 32's provenance split landed exactly as pre-measured: page_verified 19,
  linkedin_claimed 36, vendor_licensed 2, NULL 26. Pinning by hash proved load-bearing
  TWICE — once catching a dirty working tree carrying an unfinished migration, once
  catching two commits landing mid-sync.

## Current status (2026-08-09, deployed + live-tested)

- `verified` 2026-08-09 **PRODUCTION IS LIVE ON `fe56807`, SCHEMA 31.** Deployed in three
  guardian stages (preflight+backup → env vars → code+restart) with a **~4 second
  outage**. Listener PID 1227 → **12836**, one clean boot, zero tracebacks.
  `integrity_check` ok; `foreign_key_check` returns exactly the two approved
  `source_observations` orphans and no new ones. Migration 29's provenance backfill
  landed EXACTLY as predicted from the pre-measured counts: `page_verified` **19**,
  `linkedin_claimed` **36**, NULL **26**, `vendor_licensed` **0**. All four new tables
  exist and were empty. `.env` and crontab byte-identical. The write allowlist was
  proven BEHAVIORALLY (by calling `write_channel_allowed`) to be exactly
  `C01DGT9D11D,C0B02721MNK` and nothing wider. Rollback artifacts retained at
  `backups/stage1-preflight-20260809T210645Z/` and `stage3-premigration-…`.
- `verified` 2026-08-09 **THE FULL WORKFLOW WAS DRIVEN LIVE IN THE PLAYGROUND** as Chase.
  "Do you have leads in Nebraska?" → 93 leads, honestly refused to dump them in-thread;
  "just the gold ones" → 3 leads WITH lead ids, amounts, spend windows and USASpending
  verification links; "add them to a campaign" → asked which campaign rather than
  guessing; a create preview rendered with owner resolved from the roster, an explicit
  "No Leads or Campaign Members will be added in this step", an expiry, and working
  **Confirm/Cancel buttons**. The button was NOT clicked — that gate needs a human, and
  a Block Kit click cannot be driven from here. **ZoomInfo ran end to end in Slack**:
  the free preview quoted 25 people and their exact cost without spending anything, and
  an approved 2-record pull stored David Davis (Director, Technology) with his number
  while **withholding James Todd's mobile because he is do-not-call** — the safety
  property, live, in front of a rep. All three edge cases Chase named behaved: "delete
  that campaign" got an honest create-only refusal naming the real alternatives; a
  LinkedIn search for an invented person refused to substitute a real stranger under
  her name; and a phone number typed into chat was refused as evidence.
- `needs-testing` 2026-08-09 FOUR COMMITS ARE AHEAD OF PRODUCTION (the deploy pinned
  `fe56807`): the `salesforce_campaign_status` tool and its tests, plus two doc commits.
  "Who's on that campaign?" therefore still cannot be answered live until a follow-up
  deploy. Also NOT exercised: the Confirm button, `nudge --execute`, and any rich-card
  button.
- `needs-testing` 2026-08-09 the real-model acceptance matrix
  (`GRANT_LLM_ACCEPTANCE=1`, default-SKIPPED) is **22 failed / 58 passed**. *(Superseded
  2026-08-11: measured at **16 failed / 73 passed**, and 6 of those 16 flipped to
  passing on an immediate re-run — see the current status in CLAUDE.md.)* This is NOT
  a regression from this session's work: five sampled failures reproduce IDENTICALLY at
  the pre-session commit `90f0420`, and three others flipped between runs, so the suite
  is partly non-deterministic and was already failing. It is worth fixing — a
  default-skipped, flaky, failing acceptance suite gives false confidence — but it is
  its own body of work.

## Current status (2026-08-09, evening)

- `verified` 2026-08-09 EVERY NEW SURFACE EXERCISED THROUGH GRANT'S REAL DISPATCH PATH,
  not just as modules. `zoominfo_contact_preview` on lead #1 returned 3 people at
  Birmingham Community Charter High School for ZERO credits — and independently
  surfaced **Vic Chalabian, Manager of Information Technology Systems**, the same
  person the 2026-07-16 core verifier found on that school's own staff directory. Two
  sources, one answer, arrived at separately. All 3 are DNC-flagged, so their numbers
  would be withheld. `fetch_url` read a real ojp.gov page (9,549 chars) carrying its
  untrusted-content frame, and refused `http://` and `file://`. The nudge worker run
  against a production-shaped database found 2 candidates and SUPPRESSED BOTH as
  `stale` — which is precisely the guard that stops a months-old preview becoming
  Grant's first ever proactive message, the failure the critic predicted.
- `verified` 2026-08-09 PRODUCTION UPDATE, STAGED. Stage 1 preflight confirmed the
  baseline exactly (revision `90f0420`, schema 28, `integrity_check` ok, exactly the two
  approved FK orphans, listener PID 1227) and took both rollback artifacts (DB
  `63add322…fa6f`, code `62502a70…2140`). Stage 2 set the two required variables —
  `GRANT_SALESFORCE_WRITE_CHANNEL_IDS=C01DGT9D11D,C0B02721MNK` and
  `ZOOMINFO_MONTHLY_CREDITS=1000` — `.env` sha `5cb3d3b1…9df0` → `f4abd546…2a99`,
  57 → 66 lines, prefix proven byte-identical, listener and crontab untouched. The
  guardian CORRECTED my own arithmetic in that instruction (the block is 9 lines, not
  8) rather than trimming authorized content to make a derived check pass — the right
  call, and worth keeping as the standard.
- `verified` 2026-08-09 ALSO FIXED THIS SESSION: `drip --force` bypassed the daily cap
  entirely (`should_post` returned "forced" before `pacing_ok` ran), so the one command
  an operator reaches for during an incident was the only unbounded path; fixing it
  exposed a second live defect where an `unrenderable` quarantine — written BEFORE any
  Slack call — counted against the cap, so one malformed lead row silenced the product
  for a day and reported it as "daily cap reached". Campaign attempts are now recorded
  durably (migration 31) so a refused request is as visible as a successful one — the
  gap that made Nelly's dead-end invisible. Jocelyn was added to `reps.json` after
  verifying her mailbox against an exact ACTIVE Salesforce User, ending three failed
  exports. OregonBuys' withdrawn PDF now reports `SourceDocumentMoved` instead of a
  bare 404 on every poll, with NO guessed replacement URL.

## Current status (2026-08-09, later)

- `verified` 2026-08-09 **ZOOMINFO WORKS END TO END, PROVEN BY A REAL PAID CALL.** Free
  preview of Twin Rivers Unified returned 25 people (24 with email, 19 with a phone, 13
  do-not-call) for ZERO credits; a paid pull of exactly ONE record returned Robert Wilcox,
  Interim Chief Technology Officer, for 1 credit (1000 → 999). He is DNC-flagged, so his
  number was withheld while his email was kept; he stored as `vendor_licensed`, never
  `verified`; the ledger settled reserved=1 billed=1. Two bugs NO stubbed test could have
  caught, because the stub answers whatever the code asks: (1) Okta REFUSES a
  client_credentials grant naming no scope — the 400 reads exactly like a bad secret;
  (2) **`directPhone` is NOT LICENSED on this plan**, and asking for it 400s the WHOLE
  batch rather than omitting one column. Search still reports `has_direct_phone`, so a
  direct line can be seen to EXIST while being unavailable to buy — never promise one.
  Mobile numbers ARE licensed. The vendor's error body is now carried into the exception;
  losing it is what made the first failure look like an auth problem.
- `verified` 2026-08-09 SALESFORCE READ PATH LIVE against production: `lookup()` returned
  five owner-attributed Lead matches, and SOQL confirmed **"California Grant 2026" holds
  exactly 13 members** — Grant's 2026-08-06 write, verified independently of the thread
  audit. Nelly's **"California Grant 2026 - Batch 2" is still at 0 leads.** The 12
  organization-only Leads Grant created carry NO email and NO phone, which is precisely
  the gap ZoomInfo now fills.
- `verified` 2026-08-09 SHIPPED (local, branch `review/rich-award-card-campaign-20260723`,
  **16 commits, NONE DEPLOYED**): campaign slicing past 200 (a tier is cut into ordered
  batches, partition proven disjoint, no migration needed); card threads are no longer
  tool-dead; `fetch_url` with an untrusted-content frame, https-only, visible truncation,
  per-URL dedup, and a tool-loop TERMINATION so injected page text has no turn left to be
  obeyed in; ZoomInfo's two Slack tools; a deterministic removal refusal ahead of the
  model; CAPABILITY BOUNDARIES in the system prompt; and the **nudge system** (migration
  30) — four subjects, one nudge per subject ever, threaded replies only, claims
  re-verified inside the reservation, dry-run by default on a read-only connection,
  deliberately NOT in cron. `pytest` 1109 passed / 77 skipped; ruff + health clean;
  migrations reach schema 30 with integrity ok and no FK violations.
- `needs-testing` 2026-08-09 **THE PRODUCTION DEPLOY IS BLOCKED** — the permission
  classifier refused the guardian launch, so production is still `90f0420` and everything
  above is local only. Two env vars MUST be set as part of that deploy or things break:
  `GRANT_SALESFORCE_WRITE_CHANNEL_IDS=C01DGT9D11D,C0B02721MNK` (the allowlist now fails
  CLOSED, and the droplet has no such variable, so campaign writes stop without it) and
  `ZOOMINFO_MONTHLY_CREDITS=1000` (the ledger refuses every paid pull when unset).
  Migration 29 mutates data (the provenance backfill) and needs the backup-first protocol.
- `needs-testing` 2026-08-09 the live Slack workflow test in `C0B02721MNK` has NOT run,
  because the production bot already listens there and a second local bot would double-
  reply — so it must follow the deploy, not precede it.
- `needs-testing` 2026-08-09 STILL OPEN, with designs but no code: the M1 pacing fix (four
  proactive senders, one atomic primitive; `drip --force` currently bypasses the cap
  entirely), a durable record for batch attempts that fail BEFORE `_insert_manifest`
  (seven raise sites persist nothing, which is why Nelly's dead-end is invisible to SQL),
  a campaign-status tool so "who's on that campaign?" can be answered, human-asserted
  contact facts, and Jocelyn's missing `reps.json` entry.

