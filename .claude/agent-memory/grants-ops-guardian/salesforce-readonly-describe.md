---
name: salesforce-readonly-describe
description: Secret-safe way to run Salesforce describe/metadata reads on the droplet reader; sandbox confirmation; Lead record-type default trap
metadata:
  type: reference
---

Verified live 2026-07-17 over the scoped grants SSH against the droplet reader creds.

**The reader can do describe, not just /query.** `grant_watch.enrich.salesforce._readonly_get(path, params, token, url)`
builds `{instance_url}/services/data/{API_VERSION}/{path}` as a GET, so `path="sobjects/Lead/describe"`
(empty params) returns the full object describe, and `path="sobjects"` returns the global describe. No raw
`requests.get` needed — stay inside `_readonly_get` (GET-only, no writes). `_auth()` uses the reader
client-credentials app: `SALESFORCE_MY_DOMAIN_URL` / `SALESFORCE_CLIENT_ID` / `SALESFORCE_CLIENT_SECRET`.
API_VERSION default `v60.0` (overridable via `SALESFORCE_API_VERSION`).

**Run recipe (read-only, secret-safe):** deliver a Python script via ssh STDIN (never argv), run on droplet
with `cd ~/grants_agent && set -a && . ./.env && set +a && .venv/bin/python -`. Script prints ONLY metadata
(field API names/labels/types/picklists, record types). NEVER print the token, `instance_url`, host string, or
`SALESFORCE_WRITE_ORG_ID` value.

**Confirm sandbox WITHOUT leaking the host:** print booleans, e.g.
`"sandbox" in urlparse(instance_url).hostname.lower()` and `"monarchdev" in ...`. On 2026-07-17 all four
(my_domain/instance × monarchdev/sandbox) were True — reader is pointed at the **monarchdev sandbox**, not prod.

**Lead record-type default trap:** 5 active Lead record types. The one named **"Verkada"**
(DeveloperName `Verkada`) is the actual default (`defaultRecordTypeMapping=true`). Do NOT be fooled by the
record type **named "Samsara" whose DeveloperName is literally `Default`** — that is not the default mapping.
Others: `Kurrious`, `Samsara_CS`, `Samsara_Industrial`.

**Describe reflects the READER's FLS, not the writer's.** A field showing `createable=true` here does not prove
the separate write connected app/user has field-level create access — only a test create proves writeability.
