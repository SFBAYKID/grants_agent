---
name: persequor-handoff-noop
description: The shipped Phase 3 "@Persequor handoff" is a verified no-op — Persequor drops bot mentions; contacted/sent_at rows written via it are untrue
metadata:
  type: project
---

The Phase 3 approve flow (`grant_approve_send` in `grant_watch/slack/grant.py`) posts an
@Persequor mention and immediately writes `leads.status='contacted'` + `outreach.sent_at`.
Per Persequor's own agent (`~/monarch_followup_agent/persequor_integration_response.md`,
[verified] claims, 2026-07-13): Persequor **drops all bot messages**, ignores non-roster
senders, and treats the triage channel as observe-only. So the handoff never reaches
Persequor and no email is ever sent — yet Grant's DB asserts contacted/sent.

**Why:** this is a standing violation of Constitution rule 1 (never assert something
untrue) in shipped code, independent of the new workflow design. Any review of outreach
data or the Persequor integration must account for it.

**How to apply:** until the HTTP contract lands, treat every `outreach` row with
`channel='slack-thread'` and `sent_at` set, and every lead marked `contacted` by that
path, as unverified. Demand (a) immediate disable-or-relabel of the approve button and
(b) a reconciliation migration before/with the new client. See
[[workflow-design-review-2026-07]].
