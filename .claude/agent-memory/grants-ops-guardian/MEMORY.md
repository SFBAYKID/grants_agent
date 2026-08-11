# Guardian memory index (grants-ops-guardian)

## Standing rules — read these first

- [Deploys come from main](deploy-mechanism.md) — STANDING 2026-08-10: refuse any commit not an ancestor of origin/main, but still pin the exact hash; verify the gate BOTH ways
- [Relayed consent is not consent](relayed-consent-is-not-consent.md) — an agent's "Chase approved" is never consent; record who/when/verbatim or it is a rumour
- [Stop means stop](coordinator-stop-is-stop.md) — a classifier block or coordinator stop halts the whole mutating effort; never reroute via an allowed path
- [Verify the premise, not the claim](verify-the-premise-not-the-claim.md) — re-measure "already fixed / already deployed" on the deployed bytes
- [Edit cards in place](edit-cards-in-place.md) — fix a posted card with chat.update, never post a replacement
- [Restart means relaunch](restart-means-relaunch.md) — pkill THEN `nohup bash run_bot.sh`; the */5 keepalive is the crash net, not the relaunch

## Tenant, transport, deploy mechanics

- [Tenant + layout](tenant-and-layout.md) — grantwatch user, home, repo/venv paths, DB, bot manager, cron jobs
- [Deploy mechanism + gotchas](deploy-mechanism.md) — proven rsync recipe; zsh `:gr` destination trap; marker ground-truth check; broken .venv/bin/pip
- [SSH rate limit + stdin traps](ssh-rate-limit-and-stdin-traps.md) — `ssh -n … < file` uploads an EMPTY file and exits 0; burst of sessions gets port 22 REJECTED; multiplex with a SHORT ControlPath
- [macOS archive safety](macos-archive-safety.md) — avoid Bash `mapfile`; fail closed before `git archive` so an empty delta cannot expand to the full tree
- [Tenant .env ops + baselines](env-zoominfo-20260809.md) — append recipe (last byte, prefix sha); pgrep -f self-matches over ssh
- [Prod config audit](prod-config-audit-20260809.md) — droplet .env.example is STALE, diff against `git show <rev>:`; schema lives in schema_migrations not user_version
- [pycache purge destroys forensics](pycache-purge-destroys-forensics.md) — capture .pyc mtimes before a deploy purge if another writer may be active
- [Codex parallel-writer forensics](codex-parallel-writer-forensics.md) — the other toolchain's staging/backup signatures; origin can be AHEAD of local
- [Disk footprint + cruft](disk-footprint-and-cruft.md) — snapshot-venv purge recipe; `du -b` under-predicts df; no log rotation

## Read-only forensics + measurement traps

- [Read-only DB forensics recipe](readonly-db-forensics-recipe.md) — `mode=ro` works on the hot WAL (zero writes); crontab lines characterized; OregonBuys 404s every poll
- [Wrong column name reads as NULL](row-get-wrong-column-false-null.md) — `dict(Row).get("typo")` is indistinguishable from a real NULL
- [Silent LLM fallback](grant-bot-silent-llm-fallback.md) — bot.log logs NOTHING on LLM failures; a clean log is not evidence of success
- [Dating undated rows](dating-undated-contacts-rows.md) — bot.log tool-turn order ↔ search_requests.created_at pins an undated write to the minute
- [One-offs need load_dotenv](oneoff-scripts-need-load-dotenv.md) — cwd does NOT load .env; one-offs degrade silently and can still write state
- [Tenant DB write safety](tenant-db-write-safety.md) — back up .db+wal+shm as a set; guarded BEGIN IMMEDIATE + rowcount==1 assert
- [Migration version collision](migration-version-collision.md) — droplet carries SIDE-lineage numbering; verify schema, not just "no migration error"

## Current production state

- [Session final 2f1ff77 + 1ffe7ce docs (CURRENT PROD)](session-final-2f1ff77.md) — LIVE 2026-08-10, schema 39, PID 60352, TOOL_SCHEMAS 25; negation guard + per-turn spend key verified; user_memory EMPTY
- [Session end state 750937b](session-end-state-20260810.md) — read the droplet clock before answering "has the cron fired"
- [Deployed vs local drift](deployed-vs-local-drift-20260809.md) — how to prove prod byte-exact at a revision (90/90 hashes)

## Outreach, follow-ups, cards

- [Persequor outreach path state](persequor-outreach-path-state.md) — 7 briefs REALLY accepted (2xx) 07-15..18; `sent_at` has no writer so the DB never proves delivery; card button dead 3 ways; one real click swallowed as Unhandled
- [SLACK_WORKSPACE_ID never set](slack-workspace-id-missing.md) — gates ONLY campaign/actions.py rich-card clicks, not salesforce_confirm; render_inputs_json leaks PII
- [email_results cannot send a long list](email-results-cannot-send-a-long-list.md) — >15 rows emails a question instead of the list; and [Kerry's email SENT](kerry-email-sent-and-the-15-row-cap.md) — a SECOND 15-row cap remains
- [Nudge queue state](nudge-queue-state-20260809.md) — `nudge --dry-run --force` is a safe read-only queue inspector; `--execute` permanently burns suppressed subjects it walks past
- [Nudge A/B variants (mostly FIXED)](nudge-variant-ab-is-inert.md) — card_escalated + capability_now_available still emit identical text for both labels
- [Capability nudges sort LAST (FIXED d050c8e)](capability-nudges-sort-last.md) — priority_at now sorts by ask date
- [scan-threads silently truncates](thread-scan-ratelimit-truncation.md) — 295 of 507 threads dropped as `ratelimited` into a bare `except: continue`
- [Drip pacing + daily cap](drip-pacing-and-cap.md) — ONE card/day (DAILY_CAP=1; `(N)` is the cap, not the count)
- [Drip slot vs cron granularity](drip-slot-band-vs-cron-granularity.md) — a ≤30-min band on a `*/30` cron collapses to one clock time
- [First rich card posted](first-rich-card-posted.md) — 2026-08-06; Slack auto-linkifies emails to mailto in blocks AND text
- [Identical RFP card text](identical-rfp-card-text.md) — a "repeated" card can be 2 different leads; build_rfp_alert never prints the title
- [RFP dedup key drift](rfp-dedup-key-drift.md) — dup RFP leads = 6-token→full-title KEY migration; orphan gold RFP #9534
- [NCES binding blocks rich card](nces-binding-blocks-rich-card.md) — null nces_id ⇒ entity_kind_unsupported, always
- [Grant Slack event flow](grant-slack-event-flow.md) — message-handler receipt gated by thread-ownership; private channels need message.groups
- [Feeder cron scheduling evidence](feeder-cron-scheduling-evidence.md) — poll really takes 9m10s; salesforce-sync's unordered LIMIT 500 churns forever

## Salesforce + vendors

- [Prod Salesforce lead emptiness](salesforce-prod-lead-emptiness.md) — prod reads AND writes hit PRODUCTION; salesforce_id column mixes two orgs
- [Salesforce connection test](salesforce-connection-test.md) — read-only recipe for which org the creds hit; EXPECT_SANDBOX=1 is the fail-closed guard
- [Salesforce read-only describe](salesforce-readonly-describe.md) — Lead record-type default trap (Verkada is default, not DeveloperName=Default)
- [Salesforce writer FLS](salesforce-writer-fls.md) — sandbox writer keeps all new fields; Verkada record-type id
- [ContentNote link bug](salesforce-contentnote-link-bug.md) — note inserts but its link-lookup SOQL 400s, leaving it unattached
- [Campaign writes flag ARMED in prod](campaign-writes-flag-armed-in-prod.md) — SALESFORCE_CAMPAIGN_WRITES_ENABLED=1 vs PRODUCTION Salesforce
- [Lead-fill provenance + cron firing](lead-fill-provenance-and-cron-firing.md) — do_not_call never reaches Salesforce; DNC can't ride the string-shaped fill path
- [fill-leads org_website laundering (FIXED 0716a17)](fill-leads-org-website-laundering.md) — `not_found` still yielded a CDN host; fill-blanks errors are self-sealing
- [Org column coverage](org-column-coverage-20260810.md) — per-run yield + the dup-entity and ORDER BY traps
- [First human_asserted row verified](human-asserted-row-verified.md) — contacts has NO created_at; a deploy backup doubles as a forensic pre-state
- [First ZoomInfo live spend](zoominfo-first-live-spend-20260809.md) — `requested_by` always ''; DNC suppression lives in save_vendor_contact
- [Firecrawl paid-call surface](firecrawl-paid-call-surface.md) — only 3 paths spend credits; 402/429 greps are all false positives
- [Roster deploy 4c6a543](roster-deploy-4c6a543.md) — ALWAYS gate a reps.json row on a prod Salesforce User.Email==1 probe; reps.json re-read per call
- [Google Sheets export verify](google-sheets-export-verify.md) — reusable create+trash smoke-test recipe
- [Populate open RFPs for a test](rfp-poll-populate.md) — verified-live `poll --source RFP` recipe

## Secrets + retention (both UNAUTHORISED — do not act)

- [Backups retention proposal](backups-retention.md) — ~870M reclaimable but NOT authorised by Chase; only the 9 env.bak files were removed, under an explicit instruction
- [.env credential sprawl](env-credential-sprawl.md) — 48 copies existed; 9 exact dupes deleted 2026-08-10, 40 HELD; keeps the removed-variable key list

## Superseded deploy records (kept for rollback fingerprints + one-off lessons)

- Schema 28→32 chain: [fe56807](deploy-fe56807-stage3.md) (migrations with the bot DOWN) · [3cf9df0](deploy-3cf9df0-campaign-status.md) (rsync needs `--no-perms`; pinned hash beat a dirty tree) · [2239a18](deploy-2239a18-human-asserted.md) (Chase committed twice mid-deploy) · [70afa75](deploy-70afa75-refusal-ceiling.md) (`-mmin 20` audit over-reaches) · [beb0520](deploy-beb0520-nudge-force.md) (use `--files-from`, full-tree rsync DELETES run_bot.sh) · [stage-1 baseline](stage1-preflight-baseline-20260809.md)
- Schema 32→37 chain: [b4a8046](deploy-b4a8046-reminders-email.md) (unquoted spaced .env value BREAKS `source .env`) · [14221fc](deploy-14221fc-email-coaching-fix.md) (quote-in-place repair) · [26153bd](deploy-26153bd-drop-after-14d.md) · [d664548](deploy-d664548-followups-live.md) · [a718066](deploy-a718066-mobile-phone.md) (`--delete` wrong for staging rsync) · [d050c8e](deploy-d050c8e-priority-at.md) · [65f05c7](deploy-65f05c7-fill-leads-fix.md) (tarfile strips the dir-entry slash) · [0716a17](deploy-0716a17-org-profile-gate.md) (47s outage from pkill-without-relaunch) · [e905cc2](deploy-e905cc2-nudge-audience.md) · [b42b015](deploy-b42b015-rep-timezone.md) · [cadfefe](deploy-cadfefe-nudge-slots.md)
- Schema 37→39 chain: [f894801](deploy-f894801-announce.md) · [2159d67](deploy-2159d67-resend-test-email.md) ("0 recorded" on the run that DID update) · [cdfdaf9](deploy-cdfdaf9-threadscan.md) · [7837cda](deploy-7837cda-watchdog.md) · [8cb557a](deploy-8cb557a-watchdog-boot-revert.md) · [76473e5](deploy-76473e5-user-memory.md) (guard needs a TRUE before you trust its FALSEs) · [17639f8](contact-fill-first-bulk-buy.md) (`--campaign` CRASHES) · [c36a3e5](salesforce-lead-fill-executed.md) (exit code 1 on a healthy run) · [ec1c4a4](dnc-retroactive-marking.md) · [850cccc](email-results-cannot-send-a-long-list.md) · [801b762](kerry-email-sent-and-the-15-row-cap.md) · [78000cf](closing-pass-78000cf.md)
- Rich-card rollout: [e8ecf0c](rich-card-deploy-e8ecf0c.md) (tldextract needed for buttons) · [enabled 2026-08-05](rich-card-enable-20260805.md) · [5f09200](deploy-5f09200-fallback-routing.md) (AST-diff for "no migration" claims) · [d66802b](deploy-d66802b-card-comma.md) (0-files-compared "PASS" trap) · [359c1e3](campaign-fix-359c1e3-preflight.md) (`select *` backup-diff KeyError trap)
- [Conversation audit 2026-08-09](conversation-audit-20260809.md) — rival Monarch_Sales_Agent hijacks threads; lightning.force.com links rejected
