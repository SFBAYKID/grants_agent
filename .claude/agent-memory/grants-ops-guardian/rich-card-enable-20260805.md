---
name: rich-card-enable-20260805
description: GRANT_RICH_CARD_ENABLED=1 LIVE in prod since 2026-08-06T05:47Z (Chase's explicit 2026-08-05 "No just make it live", shadow gate waived); new 07:45 PT weekday rich-prepare cron; rollback = remove 1 .env line + 1 cron line
metadata:
  type: project
---

**The rich award-card campaign is ENABLED in production** (droplet revision 359c1e3, schema 28).
Authorization: Chase, 2026-08-05, verbatim "No just make it live" — given AFTER the five-business-day
shadow-gate (A4) tradeoffs were explained to him; he explicitly waived it. **This supersedes the older
"flag stays OFF / no enable" status notes from 2026-07-23/07-25** in CLAUDE.md and
[[rich-card-deploy-e8ecf0c]] / [[campaign-writes-flag-armed-in-prod]].

**Why:** Chase wants the rich cards live; the prepare worker had to come with it because without fresh
contact/activity evidence the delivery gate skips every card forever (delivery fails safe by skipping).

**How to apply:** treat "rich path live" as the production baseline from 2026-08-06 on. The daily card
now goes through `campaign/delivery.run` (NOT legacy `slack/drip.run_drip`): its OWN pacing — hardcoded
deterministic slot 10:00–10:45 PT + 11:00 PT hard cutoff (`campaign/pacing.py`) — so the legacy
`DRIP_SLOT_START_PT`/`END_PT` .env band (10:30–11:00) no longer governs the card. Rollback = delete the
`.env` line + the cron line (restore `~/.env.bak.20260806T054737Z` / `~/crontab.before.20260806T054829Z`).

Exact changes (both verified 2026-08-06T05:47–48Z, fail-closed diffs):
- `.env`: appended `GRANT_RICH_CARD_ENABLED=1` (26 bytes, 46→47 lines). sha256
  `fe9fd5888fe2279945666863cdf1374394ccab46012bb32542dfcbf6da2f3f55` →
  `b3f338ff5c42161194c6df8ee5dc1bf323dcfb0613a33a256ad034806cfc3bff`; pre-image proven intact
  (`head -c 2051 | sha256` == prior sha). Backup `~/.env.bak.20260806T054737Z` (600).
- crontab: 4→5 lines, original 4 byte-identical. Added (matches existing line style):
  `45 7 * * 1-5 cd ~/grants_agent && .venv/bin/python -m grant_watch.cli rich-prepare --execute >> cron.log 2>&1`
  (07:45 PT weekdays = after 07:00 poll, before the 10:00–10:45 rich slot). sha256
  `6275d5025455ddfa1af9dd44715fa13297b7a6a5374b9a9cf5bfc78769a44711` →
  `70e309aacb1631ad9492dc2290d62ef4aadd95aebccc8fc26c23d6261488876f`.
  Backup `~/crontab.before.20260806T054829Z`. **STALE BASELINE — superseded:** by 2026-08-09 the
  live crontab was 10 lines, sha `575fbc7c…041a72` (drift not made by the guardian; contents not yet
  characterized). See [[env-zoominfo-20260809]] for the current baseline.
- NO bot restart — verified unnecessary from source: `cli.cmd_drip` re-reads the flag per process;
  the listener never reads it; buttons bind to frozen snapshots; bot already runs 359c1e3 (PID 633555
  unchanged throughout).

Facts proven from 359c1e3 source during this run (reusable):
- Schema is recorded in `schema_migrations` (MAX), NOT `PRAGMA user_version` (reads 0) — a task
  asking for "user_version = 28" means the ledger MAX.
- 359c1e3's highest registered migration is 28 ⇒ with prod at 28, a writable `db.connect()`
  (which `rich-prepare --execute` uses) cannot advance schema.
- `rich-prepare` dry-run uses `connect_readonly` + "preview: no HTTP or writes"; `--execute` does paid
  Firecrawl contact discovery + READ-ONLY Salesforce; never posts, never writes SF
  (prepare_worker.py header). Exit 1 = indeterminate/errors present (honesty signal, not a crash);
  cron ignores it, summary goes to cron.log.
- Rich-vs-legacy drip attribution discriminator (late-evening test): legacy prints
  `skip: outside Mon-Fri 7am ET – 5pm PT window` (window gate first); rich can only print
  `skip: daily cap reached (1)` / `skip: missed the 11:00 Pacific hard cutoff` /
  `skip: waiting for today's HH:MM Pacific slot` (cap first, then cutoff/slot).

Seed run (Phase C, 2026-08-06T05:48Z→): C1 dry-run verbatim
`rich prepare: 25 candidates; 0 contact-fresh, 0 contact-refreshed, 0 activity-checked,
0 indeterminate, 0 errors, 0 local writes (preview: no HTTP or writes)` exit 0.
**C2 outcome RESOLVED 2026-08-06T07:15Z from the ledger tables (read-only), verified:** the seed's
paid work COMPLETED. 25 `paid_enrichment_attempts` rows in the window 05:49:29–05:55:32Z, all
`operation=contact_refresh`: **22 completed + 3 indeterminate** (0 stuck non-terminal).
`contact_evidence` now 22 rows total = **7 verified + 15 not_found** (not_found rows carry NULL
`first_verified_at` — count(*) vs min/max NULL-skipping can mislead; the 7 verified are all stamped
05:49:29Z). `salesforce_activity_snapshots` = 0. The verbatim `rich prepare:` C2 summary line is NOT
recoverable on the droplet — the manual run's stdout went to that session, and cron.log has no such
line (first cron-written one lands at the next 07:45 PT tick). C2's exit code is unknown (exit 1
expected with 3 indeterminate — honesty signal, not a crash). C3's slot was answered post-deploy by
[[deploy-5f09200-fallback-routing]]: `drip: skip: waiting for today's 10:41 Pacific slot`.
