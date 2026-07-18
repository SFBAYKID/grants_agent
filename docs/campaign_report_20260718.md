# Full-workflow validation campaign — final report (2026-07-18)

Owner: Chase Gonzales. Executed live in Slack (#monarch-bot-playground) against the
`monarchdev` Salesforce sandbox and the `grantwatch` droplet tenant. Every user-side
step ran through the Slack web client; the Slack API was read-only for verification.

## Verdict: PASS. Reactive runs 1–7, the proactive drip, and the autonomy audit all
passed. Fourteen defects were found *by* the campaign and fixed live, each re-verified
in Slack. 517 tests green; droplet on main `a052f11` (report commit will advance it).

## Reactive workflow (runs 1–7) — `verified` live

| Capability | Evidence |
|---|---|
| Anchored search runs immediately, plain-words grade split, per-record source links | "find me schools in California" → "269 … 99 gold … 170 watch" with a `verify this record` link pinned to each award's PortalID |
| Open-ended ask → one scoping question, never twice | "show me some leads" → one scoping question, then searches on the answer |
| Zero results guide, never dead-end | June-discovery miss offered real relaxation counts |
| Honest award-timing | "who got funding last month?" → 3 honest date meanings, no fabricated "just received" |
| Contact escalation: site → LinkedIn → org mailbox | Wichita Falls ISD returned Curtis Shahan + verified email; St Edmonds honestly none-found after all three rungs |
| Full person Lead + completed activity + Lightning Note | Wally Rakestraw #7845 (`00QVC00000Y8uEa2AJ`) SOQL-verified field-by-field |
| Persequor draft → tapped Send → test-mode email | Alief ISD intro delivered to chase@ only (Gmail-verified) |
| Guards held | pronoun traps, duplicate-record guard, compression attack, outreach refusal |

## Proactive drip — `verified` live

- Real engine posted the paced one-line nugget (Commerce ISD, $500K SVPP); next tick
  refused to repost (dedup); contextual follow-up returned Jake Rawlinson + verified
  email + Salesforce state.
- Relevance bug found and fixed: a health-sector bulletin ("Maternal Health Emergency
  Management Training") reached the channel by matching bare "emergency"; bulletins are
  now precision-first (physical-security phrase required, health terms excluded).

## Autonomy audit — `verified` read-only

- Cron (Pacific): 5-min keepalive, 30-min drip 05:00–17:30 PT weekdays, 07:00 PT daily
  poll. Six live sources, zero incomplete runs, ~9.4k new leads in the week.
- Open product decision: backfilled award events are suppressed from drip (so a 2022
  award is never announced as news), so the imported gold backlog surfaces only via
  search/polls. Not a bug — a surfacing-strategy choice for Chase.

## Fixes shipped during the campaign (14)

sort-only date searches; self-correcting tool retries (dropped the poisoning error
cache); honest tool-budget exhaustion; per-turn server logging; honest award-timing
wording; guided-search redesign (immediate anchored search + one scoping question +
zero-result recovery); compound-ask results surviving the email gate; grade split in
plain words; per-record verification links; paragraph-spacing rule; internal-identifier
ban; contact escalation chain; orphaned-spinner boot sweep; precision-first bulletins.

## Cleanup ledger (awaiting Chase)

- Sandbox test Leads to delete or keep (Chase's UI): Ben Bayle `00QVC00000Y88pm2AB`,
  Wally Rakestraw `00QVC00000Y8uEa2AJ`, plus earlier Richard Moline / ZZ FLS Probe.
- Droplet lead-status mutations from the campaign: leads #7782 (Commerce ISD) and #8956
  (Dinuba USD) moved `new → surfaced` by the drip test. Harmless (they'd surface again
  naturally); restore to `new` only if a clean drip re-demo is wanted.
- One stale `create_contact_record` action left in `ready` (never confirmed) — expires
  on its 15-min TTL; no write occurred.
- Two stray unsent drafts in Chase's own Slack composer (leftover "x" / "lead #7845"
  text from mis-targeted browser sends) — delete in the client; nothing was sent.
