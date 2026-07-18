---
name: salesforce-writer-fls
description: WRITE connected app can create Lead/Task/Note in monarchdev sandbox with ALL new fields persisting (no FLS drop); Verkada record-type id
metadata:
  type: reference
---

Verified live 2026-07-17 in the **monarchdev SANDBOX** via the WRITER gateway
`grant_watch.enrich.salesforce_campaign_gateway.SalesforceCampaignGateway` (separate writer
Connected App: `SALESFORCE_WRITE_MY_DOMAIN_URL/CLIENT_ID/CLIENT_SECRET`, scope-gated by
`SALESFORCE_WRITE_ORG_ID` + `SALESFORCE_WRITE_EXPECT_SANDBOX=1`). `create_lead/create_task/
create_note` each call `verify_write_scope()` first (org-id + IsSandbox hard gate) — a real create
proves writeability, describe alone does not (see [[salesforce-readonly-describe]]).

- **Verkada Lead RecordType id = `0122M000000viFyQAI`** (DeveloperName `Verkada`, the true default —
  see the record-type trap in [[salesforce-readonly-describe]]).
- **Full FLS probe: NO silent drop.** Created one synthetic Lead with EVERY new field set; all 17
  persisted, confirmed by BOTH the writer's own read-back (`gw._get sobjects/Lead/{id}`) AND the
  independent reader (`salesforce.readonly_soql`) — they matched field-for-field. Includes the custom
  fields **`LinkedIn__c`** (URL text) and **`Number_of_Students__c`** (number; reads back as float
  e.g. 1234.0), plus address components Street/City/State/PostalCode, Website, Industry, Description,
  LeadSource, Status. So the writer app has field-level CREATE access to every mapped field.
- `create_task` (Status=Completed, WhoId=Lead, ActivityDate) and `create_note` (ParentId=Lead,
  Title/Body) both succeeded — Task and Note are on the gateway's `_ALLOWED_CREATE_OBJECTS`.
- **Synthetic probe records left in the sandbox** (marker LastName=`FLSProbe`, Company=`ZZ FLS Probe
  School`): Lead `00QVC00000Y8Hbe2AF`, Task `00TVC00000WScnt2AD`, Note `002VC00000iOzWGYA0`. Safe to
  delete when done inspecting.

Secret-safe probe method: script via ssh STDIN, `cd ~/grants_agent && set -a && . ./.env && set +a &&
.venv/bin/python -`. Print only synthetic values, field API names, record ids, and the Lightning link
(sandbox host, needed to open the record — not a secret). NEVER print tokens or the org-id value.
