---
name: salesforce-connection-test
description: Read-only recipe to test WHICH Salesforce org the droplet creds hit (prod vs sandbox) + the EXPECT_SANDBOX fail-closed write guard
metadata:
  type: project
---

How to verify the droplet's Salesforce connection WITHOUT any write, and what the write-guard means.

**Two credential sets in the droplet `.env` (both client_credentials OAuth flow):**
- READ: `SALESFORCE_CLIENT_ID`/`_SECRET` + `SALESFORCE_MY_DOMAIN_URL`. Used by `salesforce._auth`
  → `readonly_soql()` (GET-only reader). Its own token cache.
- WRITE: `SALESFORCE_WRITE_CLIENT_ID`/`_SECRET` + `SALESFORCE_WRITE_MY_DOMAIN_URL`. Used by
  `SalesforceCampaignGateway._auth`. Separate token cache. `SALESFORCE_USERNAME/PASSWORD/
  SECURITY_TOKEN/JWT_KEY_PATH` are EMPTY (not the JWT flow).

**`gateway.verify_write_scope()` is READ-ONLY and safe to run as a connection test.** It auths with
the WRITE creds and runs `SELECT Id,Name,IsSandbox,InstanceName FROM Organization LIMIT 2`, then
asserts: endpoints HTTPS, OAuth instance host == `SALESFORCE_WRITE_MY_DOMAIN_URL` host, org Id ==
`SALESFORCE_WRITE_ORG_ID`, and `IsSandbox == (SALESFORCE_WRITE_EXPECT_SANDBOX=='1')`. It raises
`PermissionError` on any mismatch and performs **NO insert**. (Record creation happens only in
`_create_one`/`_create_many`, which CALL verify_write_scope first.)

**`SALESFORCE_WRITE_EXPECT_SANDBOX` is the production-write guard.** `=1` → writes allowed only to a
SANDBOX org; a production org (IsSandbox=false) makes verify_write_scope FAIL CLOSED, so no write can
land. Arming production writes requires deliberately setting it to `0` — a security-sensitive change;
never flip it without Chase's explicit per-run approval. See [[salesforce-writer-fls]],
[[salesforce-readonly-describe]].

**Read-only test recipe (proven 2026-07-19):** run a small snippet under the tenant venv over the
scoped ssh: `cd ~/grants_agent && .venv/bin/python -` fed a script that calls
`salesforce.readonly_soql("SELECT Id,Name,IsSandbox,InstanceName FROM Organization")` and
`SalesforceCampaignGateway().verify_write_scope()`, printing ONLY booleans / Org Id last-4 /
InstanceName / verbatim SF error bodies (SF OAuth+API error bodies carry no credential; a token error
body is `{"error":"invalid_client",...}`). GOTCHA: `load_dotenv()` AssertionErrors under `python -`
(find_dotenv walks a missing stack frame) — pass an explicit path: `load_dotenv("~/grants_agent/.env")`.

**Client-credentials auth-error decoder (observed 2026-07-19, durable meanings):**
- `invalid_grant` / `"no client credentials user enabled"` → the Connected App has NO Run-As
  (execution) user assigned for the OAuth client-credentials flow. Salesforce-side config; the
  my-domain/token endpoint is reachable, auth just has no user to run as. Chase must set the
  Connected App's client-credentials Run-As user.
- `invalid_grant` / `"request not supported on this domain"` → the token POST hit
  `https://login.salesforce.com` (or `test.salesforce.com`), NOT the org's real my-domain. The
  client-credentials flow must POST to the ORG my-domain (`https://<something>.my.salesforce.com`).
  Means `SALESFORCE_MY_DOMAIN_URL` / `SALESFORCE_WRITE_MY_DOMAIN_URL` is wrongly set to the generic
  login URL. NOTE `login.salesforce.com` classifies as "PROD-LIKE" by the naive sandbox-signal check
  (no `--`/`sandbox`), so a green signal does NOT prove a usable my-domain — auth is the real proof.

**Observation 2026-07-19 (time-stamped; will change when Chase swaps creds):** Chase believed he'd put
PRODUCTION Salesforce keys in the droplet `.env`, but BOTH read and write creds authenticated to the
**monarchdev SANDBOX** — `IsSandbox=True`, org Id last4 `R2AZ`, InstanceName `USA664S`, matching the
configured `SALESFORCE_WRITE_ORG_ID`. `EXPECT_SANDBOX='1'` and all write flags enabled
(`CAMPAIGN/PERSON_LEAD/OPPORTUNITY/ORGANIZATION_LEAD/LEAD_ENRICHMENT_UPDATES/GRANT_AUDIT` = SET) → any
writes target the SANDBOX and are fail-closed vs production. So the "production connection" was NOT
verified; auth succeeds but the environment is still the sandbox. Reported back for Chase to supply real
production connected-app creds + prod my-domain + prod org Id (and only then, deliberately, EXPECT_SANDBOX=0).

**Update 2026-07-19 (later, after Chase reworked the .env + assigned a Client-Credentials Run-As user):**
The READ path now authenticates to REAL PRODUCTION — `IsSandbox=False`, org Id last4 `8EAM`, InstanceName
`USA598` (distinct from the sandbox `R2AZ`/`USA664S`; use `8EAM/USA598`=prod vs `R2AZ/USA664S`=monarchdev-
sandbox as quick env fingerprints). Chase also removed the earlier duplicate `SALESFORCE_MY_DOMAIN_URL`.
BUT he deleted the WHOLE writer block: `SALESFORCE_WRITE_CLIENT_ID`, `_WRITE_CLIENT_SECRET`,
`_WRITE_ORG_ID`, and `_WRITE_EXPECT_SANDBOX` are now MISSING (only `SALESFORCE_WRITE_MY_DOMAIN_URL`
remains), while `SALESFORCE_CAMPAIGN_WRITES_ENABLED=1` stayed. Write path now dies with
`KeyError: 'SALESFORCE_WRITE_CLIENT_ID'` inside `SalesforceCampaignGateway._auth` (line reads
`os.environ["SALESFORCE_WRITE_CLIENT_ID"]` — NOT a graceful check) BEFORE any request → writes blocked.
Note the double fail-safe: even with writer creds present, a MISSING `EXPECT_SANDBOX` also fail-closes
(verify_write_scope requires the flag be explicitly "0" or "1", else PermissionError). System-level check:
NO Salesforce vars in any tenant shell profile (all absent) or run_bot.sh — all SF config is `.env`-sourced;
the live bot (pid from the ba0a7b7 deploy) still holds the OLDER fuller sandbox env in memory (env changes
apply only on restart, which was NOT done). Incoherent-config flag for Chase: CAMPAIGN_WRITES_ENABLED=1 with
no writer creds + no EXPECT_SANDBOX — should either drop the enable flag or re-add the full prod writer set
with a deliberate EXPECT_SANDBOX=0 (him watching) before arming prod writes.

**PROD WRITE CUTOVER WENT LIVE 2026-07-20 (verified read-only, no record created).** Code `aa09dca`
("refactor: writer SF creds fall back to reader") added `_write_client_id/secret/my_domain()` helpers:
each uses `SALESFORCE_WRITE_*` only if NON-EMPTY, else falls back to the reader
(`SALESFORCE_CLIENT_ID/SECRET/MY_DOMAIN_URL`). Chase (manually, outside Claude Code) deployed it, then
set the droplet `.env` to: REMOVE `SALESFORCE_WRITE_MY_DOMAIN_URL` (was the bad `login.salesforce.com`
→ now falls back to the prod reader my-domain), NO `SALESFORCE_WRITE_CLIENT_ID/SECRET` (fall back to reader),
ADD `SALESFORCE_WRITE_ORG_ID`=prod `00D41000002jIQ8EAM`, and `SALESFORCE_WRITE_EXPECT_SANDBOX=0` (ARMS prod
writes). Verified: no duplicate SF keys; read path IsSandbox=False / org 8EAM / USA598; **verify_write_scope
PASSES for production** (is_sandbox False, org matches, EXPECT_SANDBOX=0). So writes are armed — but every
write still passes verify_write_scope AND requires a human Slack approval; the guardian creates NO records.
CAVEAT: migration 13 was PENDING at cutover (restart doesn't migrate — see [[deploy-mechanism]]).
