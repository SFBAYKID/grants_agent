---
name: salesforce-contentnote-link-bug
description: create_content_note creates the ContentNote but its link-lookup SOQL 400s in monarchdev, so the note is never attached to the target record
metadata:
  type: project
---

Verified live 2026-07-18 in the **monarchdev SANDBOX** while running the deployed `84002a2`
`SalesforceCampaignGateway.create_content_note` against Ben Bayle's Lead `00QVC00000Y8SaB2AV`.

**Symptom:** `create_content_note(...)` returned `CreateResult(success=False,
record_id='069VC00000KEk2gYAD', error='note created but link lookup failed: HTTPError')`. The
ContentNote WAS created, but the ContentDocumentLink to the target Lead was NOT — so the note does
NOT show in the Lead's modern Notes list (goal unmet).

**Root cause (verified by SOQL):** inside `create_content_note`, after inserting the note it runs
`SELECT ContentDocumentId FROM ContentNote WHERE Id='069...'` to translate note.Id → ContentDocumentId.
That query returns **HTTP 400** in monarchdev (API v60.0). The same `ContentNote`-with-`Id IN (subquery)`
shape also 400s (the task's own verify Q2 failed identically). So the translate step raises
`requests.HTTPError`, the except returns failure, and the link create is skipped.

**The fix is trivial** and proven by read-back: a ContentNote's **own Id already equals its
ContentDocumentId**. Evidence: `SELECT Id, FileType FROM ContentDocument WHERE Id='069VC00000KEk2gYAD'`
returns one row, `FileType='SNOTE'`, SAME id. So `create_content_note` should pass `note.record_id`
directly as the `ContentDocumentId` for the ContentDocumentLink and DROP the failing SELECT entirely.
(Owner code fix — the guardian does not edit product source.)

**Salesforce auto-author link gotcha:** a freshly inserted ContentNote is NOT link-less — Salesforce
auto-creates ONE ContentDocumentLink to the *author/running user* (a `005…` id, `ShareType='I'`,
`Visibility='AllUsers'`). So "note has a link" ≠ "note is on the target record"; always filter
`ContentDocumentLink` by `LinkedEntityId=<target>` (a `00Q…` Lead here), which was **0 rows**.

**Orphan left in sandbox (safe to delete or link, needs owner call):** ContentNote/ContentDocument
`069VC00000KEk2gYAD`, Title `Grant lead: DEKALB COMMUNITY UNIT SCHOOL DISTRICT #428 — Ben Bayle`,
linked only to its author `005...dACEAA2`. The scoped one-write repair (awaiting approval) is a single
`gw._create_one("ContentDocumentLink", {ContentDocumentId:'069VC00000KEk2gYAD',
LinkedEntityId:'00QVC00000Y8SaB2AV', ShareType:'V', Visibility:'AllUsers'})` — reuses the existing
note (honors one-note-max), no second ContentNote. See [[salesforce-writer-fls]].

**Verify-query caveat:** to list notes on a record, do NOT use `ContentNote WHERE Id IN (subquery)`
(400 here). Two-step it: `SELECT ContentDocumentId FROM ContentDocumentLink WHERE LinkedEntityId=<id>`
then `SELECT Id,Title,TextPreview FROM ContentNote WHERE Id IN ('<doc1>','<doc2>')` with literal ids.
