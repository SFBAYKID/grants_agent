---
name: prod-config-audit-20260809
description: Full read-only prod config audit at 26153bd — all 32 .env keys non-empty and all 32 present in the live process; the only guard actually OFF is SALESFORCE_WRITE_EXPECT_SANDBOX=0; the droplet .env.example is STALE so never diff against it
metadata:
  type: project
---

Read-only audit 2026-08-09T23:20Z answering Chase's standing worry: *"I've been told a feature was
configured on the droplet when it was actually switched off."* Verdict: **nothing is set-but-empty,
and nothing is in-file-but-not-in-process.** Recording the two real findings and the traps.

**Why:** Chase has been burned repeatedly by a feature reported as live that was inert. He wants
evidence, not assurance. **How to apply:** re-run this shape before believing any "it's configured"
claim; lead the answer with the two findings below, not with the clean parts.

## The two findings that are genuinely "off"

1. **`SALESFORCE_WRITE_EXPECT_SANDBOX = 0`** — the only safety flag in a disabled position. Memory
   [[tenant-and-layout]] recorded it as `1` on 2026-07-17; it is `0` now, so
   `salesforce_campaign_gateway.py:242-270` asserts the writer org is **PRODUCTION** Salesforce.
   That is deliberate (CLAUDE.md: prod campaign writes have fired) but it IS the guard being off,
   and it is the one to name first. The gateway still hard-fails if the value is not literally
   `0`/`1` and if `SALESFORCE_WRITE_ORG_ID` is unset — both are set, so writes are armed, not open.
   **The `.env` line carries a TRAILING SPACE (`0 `, 2 chars) while `/proc/environ` shows 1 char** —
   python-dotenv strips it, and the gateway `.strip()`s again, so it is inert here. But a 1-char
   vs 2-char length mismatch between `.env` and environ is a real signal worth checking, not noise.
2. **`SLACK_WORKSPACE_ID` still absent** — see [[slack-workspace-id-missing]], whose scope I
   corrected in the same pass (it gates rich-card actions ONLY, not `salesforce_confirm`).

## THE TRAP: the droplet's own `.env.example` is STALE — never diff against it

Droplet `.env.example` sha `8fb7b1f5…` / 74 lines / **32 keys**; repo at the deployed revision
`26153bd` sha `a8b2830d…` / 100 lines / **37 keys**. Surgical `--files-from` deploys
([[deploy-26153bd-drop-after-14d]], [[roster-deploy-4c6a543]]) never ship `.env.example`, so the
droplet copy lags by many commits. Diffing prod `.env` against the DROPLET copy would have HIDDEN
5 of the 10 missing keys, including `SLACK_WORKSPACE_ID` and `GRANT_MODEL`. **Always diff against
`git show <deployed-rev>:.env.example` locally**, then pull the prod key NAMES down with
`grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' .env | tr -d '=' | sort -u` and `comm` the two locally.

## A missing key is only a defect if the code default disables something — check, don't assume

Of the 10 keys in `.env.example@26153bd` but absent from prod, **9 are benign by code default**
(verified by grepping the deployed tree, not the local one): `GRANTS_DROPLET_{HOST,USER}` are
laptop-only; `GRANT_MODEL`→`DEFAULT_MODEL`; `SALESFORCE_API_VERSION`→`v60.0`;
`GRANT_WATCH_STATES` empty = **nationwide** (narrowing, not widening); `GRANT_TERRITORY_OWNERS`
empty = the CA/OR/PA/TX/WA defaults; and `SALESFORCE_WRITE_{CLIENT_ID,CLIENT_SECRET,MY_DOMAIN_URL}`
deliberately **fall back to the reader credentials** (`salesforce_campaign_gateway.py:100-130`,
Chase 2026-07-19 — one Connected App for both is the intended setup). Only `SLACK_WORKSPACE_ID`
fails closed. Report the count of missing keys AND what each one's default actually does, or the
list reads as ten alarms when it is one.

Conversely 5 keys are in prod but not in `.env.example` (`DATABASE_URL` + four
`SALESFORCE_*_ENABLED` flags) — `.env.example` is the thing that is behind, not prod.

## Baselines confirmed unchanged (cheap re-verification anchors)

`.env` sha `f4abd546…2a99` / 66 lines / 3334 B / mode 600 / **32 keys, zero empty**.
crontab sha `575fbc7c…1a72` / 10 lines = 5 active jobs + 5 comments, **no `nudge` line** (grep -c 0,
still never scheduled). Revision `26153bd`, schema **32** (read from `schema_migrations`, NOT
`PRAGMA user_version` — that is **0** and always has been; using it would report "schema 0").
42 tables. `integrity_check` ok; `foreign_key_check` exactly the two known `source_observations`
orphans (10642, 11892). Listener PID 19225, uid 1001, exactly one. Disk 67%, 17 G free, home 2.1 G.

**Every one of the 32 `.env` keys is present in `/proc/19225/environ`** — the bot restarted at
23:11:42Z for the `26153bd` deploy, after the last `.env` write (14:12 PDT), so the process is
current. The only environ-extra keys are 18 standard shell/session vars.

## ZoomInfo spend, first month

`zoominfo_credit_periods` has ONE row: period `2026-08`, limit 1000, **consumed 2**. One settled
spend row, 2 reserved / 2 billed, lead 4897 — the Scottsbluff paid pull. `requested_by` is `''`,
the known gap from [[zoominfo-first-live-spend-20260809]]. So live testing cost **2 of 1000**.

## Log ground truth

`bot.log` 13 tracebacks, **all `CompletedPaidCall` in `find_contact`, all at lines ≤ 870 while the
last 8 boot banners start at line 902** — i.e. every one predates the deploy of the `3adebba` fix.
**Zero errors since the current boot.** `ERROR` count in bot.log is 0; the marker there is
`[tool-error]`, so grepping only for `ERROR` would report a false all-clear.
`cron.log`: 7048 healthy keepalives / 42 restart_attempts, 0 tracebacks; its 61 `ERROR` lines are
11 real OregonBuys 404s + 1 USASpending ReadTimeout, and the rest are **false positives — lead
titles containing the word "TERRORIST"** matching `grep ERROR`. Anchor the pattern or you will
report 49 phantom errors.

**Weekend gotcha:** all cron jobs are `1-5` (weekdays). Auditing on a Sunday, the newest poll/drip/
rich-prepare output is Friday's — that is correct, not a stalled pipeline. Last drip did post:
`drip: posted rich_award for lead #7789: BARTLETT INDEPENDENT SCHOOL DISTRICT`, then the expected
`daily cap reached (1)` for the rest of the day.
