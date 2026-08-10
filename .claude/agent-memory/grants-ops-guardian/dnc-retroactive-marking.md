---
name: dnc-retroactive-marking
description: Deploy c36a3e5→ec1c4a4 (DNC warning leads every evidence class, verified on real rows) and the retroactive gap — 2 live Salesforce Leads name a do-not-call person with no marker; why fill_lead_blanks structurally cannot fix it and which primitive to add instead
metadata:
  type: project
---

**Deploy LIVE 2026-08-10T08:06Z.** `c36a3e5` → `ec1c4a4eaff971367ffaf59c07bb3a5c3a567532`.
Listener **41662 → 42839**, **0.142 s outage**. Schema **39**, no migration. 2 files, both
modifications. Closure **123/123**, import closure 120/120, `.env` + crontab byte-identical,
0 tracebacks. Backup `~/backups/deploy-ec1c4a4-20260810T080615Z/`.

## The guard fires on what SQLite actually stores

`_contact_evidence` tests `_row_value(contact,"do_not_call") in ("1", 1, True, "True")`.
SQLite stores `('integer', 0)` / `('integer', 1)` — verified by `typeof()` — so the `1`
branch is the one that matches. Exercised against **all 6 real `do_not_call=1` rows**:
every one now leads with `DO NOT CALL: …`, and two `do_not_call=0` controls do not.
Worth doing behaviourally: a membership test against a literal tuple is exactly the kind
of thing that silently never matches if the stored type differs.

## THE RETROACTIVE GAP — 2 Leads, not 1

The fix composes the Description at CREATION time, so existing records are untouched.
Audited all 21 Leads with a `salesforce_id`; the **selected** contact is do-not-call on:

| lead | Salesforce Id | company | contact |
|---|---|---|---|
| #231 | `00QUZ00000byrvN2AQ` | BIRMINGHAM COMMUNITY CHARTER HIGH SCHOOL | id 86 |
| #237 | `00QUZ00000byrvT2AQ` | MAMMOTH UNIFIED SCHOOL DISTRICT | id 88 |

Mammoth was not in the brief — only Birmingham was. Both Descriptions are **261 chars and
contain no do-not-call language**. Both have `Phone` populated and `MobilePhone` empty.

**The compounding detail:** that `Phone` is the ORGANIZATION switchboard (`lead.org_phone`),
not the person's line, so dialling it is not itself a breach of an individual's DNC flag.
But it sits directly beneath that person's Title and Email on the record, so a rep would
reasonably dial it and ask for them by name. Same shape as the switchboard-reads-as-a-
direct-line defect already fixed once in `salesforce_contact_records`.

## Why `fill_lead_blanks` structurally cannot fix it

`_ALLOWED_LEAD_FILL_FIELDS` = Street, City, State, PostalCode, Phone, MobilePhone, Email,
Title, Website, Industry, Number_of_Students__c — **11 fields, no `Description`.** And
even allowlisted, every Grant-created Lead already HAS a Description, so a fill-a-blank
operation would skip it.

**`DoNotCall` cannot ride that path either, and the reason is subtle:** the filter is
string-shaped — `str(value or "").strip()` on the proposal, then "send only what
Salesforce currently holds nothing for". For a boolean, `str(False)` is `"False"`, which
is non-empty, so a currently-false checkbox reads as **already populated** and is skipped.
Adding `DoNotCall` to the allowlist would therefore do nothing until the emptiness
semantics changed — and those semantics are what protect all 11 existing fields.

## Recommendation given (no code written, nothing executed)

1. **Preferred: the standard `DoNotCall` checkbox**, via its own ~10-line primitive —
   read `DoNotCall`, set true only if currently false. Idempotent by construction, cannot
   truncate anything, takes no caller-supplied content, and lands the fact where
   Salesforce's own dialer and campaign tooling can act on it instead of in prose.
   **Blocker to settle first: `SELECT DoNotCall FROM Lead` returns HTTP 400 for this
   integration user**, which points at missing field-level security. That is a Salesforce
   setup question and must be answered before building, or the primitive 400s too.
2. **Fallback if FLS cannot be granted: a narrow append-only Description primitive** with
   three properties — reads first and asserts the existing text is a strict PREFIX of what
   it writes (truncation structurally impossible, not merely intended); idempotent on the
   marker (a re-run must not double the sentence); and accepts only a fixed, code-owned
   string, never caller text. "Mark this lead do-not-call" is a far smaller write surface
   than "append arbitrary content to Description".
3. **Not a Note.** [[salesforce-contentnote-link-bug]] measured `create_content_note`
   creating the note but failing to link it (link-lookup SOQL 400s), leaving it
   unattached — a compliance warning that exists but is invisible is worse than none. A
   Note also needs a click, which contradicts the "first, not scrolled-to" reasoning the
   fix itself is built on.
4. **Not "leave them".** Only two records, but they are the two where the risk is live
   today; marking only future records protects the hypothetical and not the actual.

Related: [[lead-fill-provenance-and-cron-firing]], [[salesforce-lead-fill-executed]],
[[salesforce-contentnote-link-bug]], [[contact-fill-first-bulk-buy]].
