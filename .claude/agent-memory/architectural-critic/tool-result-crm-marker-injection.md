---
name: tool-result-crm-marker-injection
description: conversation.py harvests <grant-crm-action> markers from TOOL RESULTS, not just model text — any tool returning external content can manufacture a Salesforce approval button
metadata:
  type: project
---

`conversation.py:851` runs `_extract_pending_actions(text)` on the **tool result string**
returned by `tools.run_tool`, then extends `pending_actions` (`:852`). `grant.py` renders
those into real Slack blocks with a primary "Confirm in Salesforce" button and
attacker-controlled `preview` mrkdwn (`grant.py:87-149`, called at `:581` and `:753`).
The regex `_CRM_ACTION_RE` (`conversation.py:407`) is also used to STRIP the marker, so the
model never sees it and cannot report the injection.

Today this is safe only because every tool that returns external text (`web_search`) returns
a compact reformatted summary, not raw page content. **Any new tool that returns raw
external content — `fetch_url`, a PDF reader, an email body — opens it.**

The click itself fails (`salesforce_confirm` → `confirm_action` raises ValueError on an
unknown action_id → "Salesforce was not changed", `salesforce_actions.py:220-229`), so no CRM
write. The damage is (a) unsanitized attacker mrkdwn incl. links in Grant's voice in the
production channel, (b) training reps to click approval buttons sourced from web pages,
(c) rule-1: Grant asserts a preview that does not exist.

**Why:** structured control data is being smuggled through a model-visible string channel.
`run_tool` already returns a tuple — actions belong in a third element, not in the text.

**How to apply:** reject any design that adds a tool returning raw external content until
either (i) markers move out of the string channel, or (ii) `_extract_pending_actions` is
applied ONLY to an allowlist of trusted producer tools AND the `action_id` is verified to
exist in `crm_actions` bound to this workspace/channel/thread/requester before a button
renders. Related: [[fetch-url-design-review-2026-08]].
