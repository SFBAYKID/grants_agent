---
name: env-credential-sprawl
description: 48 copies of the production .env existed in the grants tenant home (not the ~10 first estimated); 9 exact copies of the CURRENT credentials were deleted 2026-08-10, 40 are held pending Chase's call; includes the removed-variable key-name list worth preserving before they go
metadata:
  type: project
---

## The real size of it

Enumerated 2026-08-10 (read-only, names and shas only — never a value). Not ~10 copies:
**48 credential files** in the tenant home, because the RETIRED `cp -a` deploy recipe
copied whole trees **including `.env`**, so every `.grants_agent.previous.*` snapshot
carries one.

| location class | count |
|---|---|
| `.grants_agent.previous.*/.env` (+ one `.env.bak.pre-604069d`) | 24 |
| `~/.env.bak.<UTC>` at home root | 8 |
| `backups/env_before_*` + `env.bak.stage2-*` | 5 |
| `backups/deploy-*/env.bak` | 11 → **2 after cleanup** |
| `.grants_backups/*/prod.env` | 1 |

`.env.example` files are templates with no real values and are **not** in scope.

## What was deleted (2026-08-10)

**9 files**, all `backups/deploy-*/env.bak` from the 2026-08-09/10 deploys, each verified
before removal to be: env-shaped (≥30 keys), a **strict subset of the live key set**
(`extra=0`), under the sanctioned `backups/deploy-*/env.bak` path, and not the live file.
All nine were **byte-identical to the live `.env`** — i.e. exact copies of the *currently
valid* Slack bot token, Resend key, ZoomInfo secret and Anthropic key. Highest-value
removal available. Live `.env` sha `9b68bc18…c634` / 33 keys / mode 600 verified
identical before and after; all 23 deploy directories left intact.

## What is HELD, and why

The other **40** all carry key names that do **not** exist in the live `.env`. Chase's
standing instruction is to be told before deleting one of those, because a variable that
survives only in an old copy is a clue about something that was removed. Every extra key
turned out to be explainable, and the complete distinct set is:

```
SALESFORCE_JWT_KEY_PATH            SALESFORCE_USERNAME
SALESFORCE_LOGIN_URL               SALESFORCE_PASSWORD
SALESFORCE_SANDBOX_NAME            SALESFORCE_SECURITY_TOKEN
SALESFORCE_WRITE_CLIENT_ID         SALESFORCE_WRITE_CLIENT_SECRET
SALESFORCE_WRITE_MY_DOMAIN_URL     SALESFORCE_ORGANIZATION_LEAD_WRITES_ENABLED
SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED
ZOOMINFO_ACCESS_TOKEN
```

All twelve are known, deliberate removals: the old username/password + security-token
Salesforce auth (replaced by JWT/client-credentials), the sandbox-write config
([[deploy-2159d67-resend-test-email]] records prod has no `SALESFORCE_WRITE_MY_DOMAIN_URL`),
two dead feature flags (`SALESFORCE_LEAD_ENRICHMENT_UPDATES_ENABLED` gates no code
anywhere), and the 24-hour ZoomInfo token dropped as dead weight.

**`SALESFORCE_PASSWORD` and `SALESFORCE_SECURITY_TOKEN` sit in ~35 of these files.** That
is a real user-account credential, not just an API key, and it is the strongest argument
for clearing the rest.

## The recommended way to close it

**Preserving the key-name list above preserves the entire clue without preserving a single
secret value.** With that recorded here, the 40 remaining files can be deleted with no
loss of information. Proposed order:

1. `~/.env.bak.*` (8) and `backups/env_before_*` + `env.bak.stage2-*` (5) — loose, oldest, no other purpose.
2. `.grants_agent.previous.*/.env` (24) — delete the `.env` FILE only; the snapshot dirs
   are a separate decision ([[backups-retention]]), and removing a `.env` from a snapshot
   costs nothing since the live `.env` is authoritative and must never be restored from one.
3. `.grants_backups/*/prod.env` (1) and the last 2 `backups/deploy-*/env.bak`.

**Rotation is the other half.** Deleting copies does not un-leak a token that was already
copied 48 times over four weeks. Whether to rotate the Slack bot token, Resend key,
Salesforce secrets and ZoomInfo credentials is Chase's call, not the guardian's, and it
is worth asking explicitly.

**Why this keeps happening:** deploys used to take an `env.bak` reflexively. Current
practice is to take **none** when `.env` is untouched — that is why the last four deploys
added zero new copies.

Related: [[deploy-mechanism]], [[backups-retention]], [[prod-config-audit-20260809]],
[[env-zoominfo-20260809]].
