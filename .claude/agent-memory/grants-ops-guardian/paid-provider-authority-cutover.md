---
name: paid-provider-authority-cutover
description: The 2026-08-13 Firecrawl/ZoomInfo authority cutover - authority id, ledger paths, the 7 .env keys, and why droplet spend totals are a FLOOR
metadata:
  type: project
---

**Paid spend now requires a host-local capability file.** Cut over 2026-08-13 with
[[prod-state-58b3e24-verified]]. Runbook: `docs/paid_provider_cutover.md`.

**Why:** an API key copied to a laptop must not carry spend authority with it. Runtime
binds each standalone ledger to a random authority id + a per-vendor account scope in an
owner-only file outside the deploy tree.

**How to apply:** never recreate or copy these files; never "fix" a refusal by loosening a
permission, a ceiling, or the metadata binding. If the runtime check refuses, investigate.

## The facts (identity labels, not secrets)

- Authority id: **`auth-617b8e82f23c4865be706535e867dfa0`**
- `/home/grantwatch/private/` **mode 700**; all three files **mode 600**, none symlinked:
  - `paid-provider-authority.json` (scopes `monarch-firecrawl-acct-01`, `monarch-zoominfo-acct-01`)
  - `firecrawl-runtime-ledger.db`
  - `zoominfo-credit-ledger.db`
- The init command is **no-replace** - a second `--execute` returns
  `refused: refusing to replace an authority file` (proven, exit 2).

## The 7 `.env` keys added (all non-secret)

`GRANT_PAID_PROVIDER_MODE=authority`, `GRANT_PAID_PROVIDER_AUTHORITY_FILE`,
`FIRECRAWL_RUNTIME_LEDGER_PATH`, `FIRECRAWL_RUNTIME_MONTHLY_CALL_LIMIT=3000`,
`FIRECRAWL_RUNTIME_REQUESTS_PER_MINUTE=20`, `ZOOMINFO_CREDIT_LEDGER_PATH`,
`SLACK_WORKSPACE_ID=T01DFJLFKE3`.

**`SLACK_WORKSPACE_ID` was read from `auth.test`, never guessed** - team `Monarch`, bot
`U0BH0ESRJ4W`. If it is ever needed again, read it; do not copy it from a URL.

**Append, never rewrite, and take NO `.env` backup.** The file ends in a newline, so append
and then prove the original prefix bytes are unchanged by sha. A `.env.bak` would be
another credential-bearing copy and the droplet already holds 64 `.env*` files
([[env-credential-sprawl]]). Copy count was 64 before and 64 after.

## The startup gate is real, and it fails closed

`grant_watch/slack/grant.py:917` raises on any `runtime_configuration_issues()`. On the real
production `.env` the NEW code reported **exactly 6 blocking issues**; deploying the code
without the 7 keys would have taken Grant offline on restart. Proven in **both directions**
on the deployed bytes - clean on the live env, and refusing for: mode flipped to `disabled`
with credentials present, authority file pointed elsewhere, a relative authority path,
`SLACK_WORKSPACE_ID` removed while rich cards are on, and a rate of 601.

Note the gate's deep branch: when called with **no argument** it also *opens* both ledgers,
so a malformed ledger stops the listener rather than the first Slack request. Passing an
explicit mapping skips that - which is what makes a bare preflight script a weaker check
than the real thing. Run `python -m grant_watch.paid_provider_runtime_check` for the full one.

## THE HONEST LIMIT - droplet totals are a FLOOR, not an account total

**Chase declined rotation, 2026-08-13, verbatim: "Just leave the api keys alone. Lets push
this to pproduction. The api keys are fine."** So:

- The **laptop still holds working `FIRECRAWL_API_KEY`, `ZOOMINFO_CLIENT_ID` and
  `ZOOMINFO_CLIENT_SECRET`**, and 40 held `.env` copies remain on the droplet. The droplet
  is therefore **NOT the exclusive spend authority**, and the runbook's cutover is
  deliberately incomplete at step 5. **Exclusivity is not claimed.**
- ZoomInfo ledger holds **7 settled spends / 14 credits of 1000 for 2026-08**, all
  `source_scope='production'`. The known laptop history (**2 spends / 3 credits**) was
  **NOT merged** - the laptop is not being retired, so merging it would assert a cutover
  that did not happen. True same-account figure is therefore ~9 spends / 17 credits.
- **The Firecrawl ledger starts EMPTY, and that zero is not "no Firecrawl spend ever".**
  The `firecrawl_runtime_*` tables are created by migration 42, so they did not exist
  before this deploy - all prior Firecrawl spend was **unledgered entirely**. Preview and
  execute both reported `periods=0, attempts=0, reserved=0, reconciliation_required=false`.

**Always write "droplet-observed spend" in any report or doc line.** A number from these
ledgers under-counts the account and must never be presented as a total.
