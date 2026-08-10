---
name: email-results-cannot-send-a-long-list
description: Deploy 750937b→850cccc, and the STOP that followed — email_results cannot deliver a result set larger than 15, it emails "would you like an Excel file?" instead; plus the 22-tool inventory and which capability_asks have no tool behind them
metadata:
  type: project
---

**Deploy LIVE 2026-08-10T21:12:38Z.** `750937b` → `850cccc5f8c2b542e91080bfcd7220a417e77197`.
Listener **43889 → 55908**, **0.195 s outage**. Schema **39**, no migration. Closure
**123/123**, import closure 120/120, `.env` + crontab byte-identical, 0 tracebacks,
watchdog tick confirmed after the restart.

**My own error, recorded because it nearly cost a rollback artifact:** I built the deploy
script by `sed`-templating the PREVIOUS deploy's script. The substitution produced
`grant_watch/enrich/slack/grant.py`, so `tar` failed and the rollback tarball shipped
WITHOUT `grant.py`. Caught by reading the output rather than the exit status. Repaired by
uploading the true `750937b` blob (`a24bb973…`) into the backup dir. **Never sed-template a
target-specific deploy script** — [[deploy-mechanism]] already says never reuse them.

## THE STOP — `email_results` cannot send a rep a long list

Kerry asked on 23 July for Texas schools under SVPP and NSGP; Grant exported 81 + 14 to
Excel in-thread, she said "Email those to kerry@monarchconnected.com", Grant could not.
Today's nudge reopened it, she replied "Yes", and 850cccc fixes the routing.

**But the delivery cannot work.** `search_leads` has this branch
(`grant_watch/slack/search.py:669`):

```python
if total > 15 and int(limit or 50) > 15 and not export_value and not with_contacts:
    return f"Found {total} matches. That's a large result set — would you like an Excel file or a Google Sheet?"
```

`lead_digest.render` — the renderer `email_results` deliberately shares — returns that
sentence verbatim. Rendered for real, **every** variant produced the same 93 characters:

| spec | rendered body |
|---|---|
| TX/school/SVPP, scope=all | `Found 81 matches. That's a large result set — would you like an Excel file or a Google Sheet?` |
| TX/school/SVPP, top_n limit=100 | identical |
| TX/school/NSGP, any | `Found 18 matches. …` |

It passes both checks an operator would think to run — no `NO_MATCH` fallback, no
`<model-note>` scaffolding — **and contains zero leads.** Sending it would have emailed a
QUESTION to the one rep whose entire complaint is being asked questions instead of served,
in a medium where she cannot even reply. That would have been the fourth non-answer in one
thread.

**Proof the renderer itself is fine:** at `limit=15` it returns 4,331 chars of real leads
with ids, amounts, spend windows and USASpending verification links. The failure is
exclusively the `>15` branch.

**And the file cannot be attached.** `resend_client.send_to_rep(slack_user, subject,
text_body, *, html_body, dry_run, session)` has no attachment parameter, `resend_client`
never mentions "attach", and `lead_digest.render` discards the artifact
(`text, _artifact = search_leads(...)`). So `export="excel"` produces a file nobody sends.

**Net: `email_results` can deliver (a) a question, (b) a silently truncated 15 of 81, or
(c) an orphaned export. None is what she asked for.** Nothing was sent and no Slack reply
was posted — telling her "sent" would have been false, and anything else re-asks her
something.

Counts today: **SVPP 81, NSGP 18** (was 81/14 on 23 July — NSGP grew by 4).

Two fixes, smallest first: let the digest know its DESTINATION so the >15 guard applies to
Slack (where dumping 81 leads is wrong) and not to email (where a long list is the whole
point); or give `send_to_rep` an attachment and pass the Excel artifact through, which is
literally what "email those" meant.

## Tool inventory, deployed revision: 22 tools

`email_results, fetch_url, find_contact, find_person_linkedin, lead_stats,
record_contact_fact, reminder_cancel, reminder_list, reminder_set,
salesforce_campaign_batch_preview, salesforce_campaign_create_preview,
salesforce_campaign_members_preview, salesforce_campaign_search,
salesforce_campaign_status, salesforce_contact_record_preview, salesforce_lookup,
search_leads, source_inventory_status, stop_followups, web_search,
zoominfo_contact_preview, zoominfo_enrich_contacts`

`capability_asks` is **34 rows, not 20** — the Monday 04:40 scan-threads cron added 14.
5 armed, 29 unarmed across 19 slugs.

**No tool behind them — arming would be a false promise:**
- `filter_by_application_status` — "who APPLIED for COPS". Grant holds opportunities,
  solicitations and awards; application status is not in any source. Never arm this.
- `format_spreadsheet_for_dataloader` / `format_spreadsheet_for_upload` — a Data
  Loader-shaped CSV; `export` emits a generic sheet.
- `direct_lead_field_edit` — no Slack tool, and the only fill path is blanks-only.

**Served only by an OPERATOR CLI, not by anything Grant can call** — the trap tier:
`bulk_contact_enrichment`, `campaign_member_enrichment` (fill-contacts / fill-leads are
CLI). Arming these makes Grant promise work a human must run.

**Genuinely served by an existing tool:** `salesforce_lookup`, `contact_lookup`,
`search_scoping`, `filter_by_award_date`, `contact_phone_mobile_enrichment`, and the whole
campaign family (`add_leads_to_campaign`, `load_leads_to_campaigns` ×9,
`salesforce_campaign_add`, `salesforce_batch_upload`, `salesforce_upload`,
`add_campaign_members_via_ids`, `pull_lead_ids_for_campaign`, `create_salesforce_campaigns`)
— via the preview + human-confirm flow, proven live on 2026-08-10.

## FOURTH false zero from an anchored/hyphenated grep

`nces-bind` reported 0 runs. The real signature is **`nces bind:`** — a SPACE, and the cron
command uses a hyphen. It had run: `168/500 leads bound across 12 state(s); 0 failed`, and
`leads.nces_id` went 172 → **340**. Previous three: `rich prepare:` (space), `nces` matching
"Sciences", `announce` matching "Announcement". **Derive the signature from the log, never
from the cron command.**

## Monday's proactive stack — it all fired

announce posted 15:00:04Z (08:00 PT), `slack_ts` 1786374005.238569. remind 13 runs.
scan-threads 1 run. nudge 26 runs, **2 delivered**: Kerry (`email_results`, variant a,
engaged 10:03) and **Jocelyn U06RXJKRXSR (`campaign_load`, variant b, 14:15 PT, not yet
engaged)**. `user_memory` still 0.

Related: [[session-end-state-20260810]], [[lead-fill-provenance-and-cron-firing]],
[[deploy-mechanism]].
