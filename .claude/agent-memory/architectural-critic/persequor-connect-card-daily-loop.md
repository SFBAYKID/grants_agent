---
name: persequor-connect-card-daily-loop
description: Cross-repo (monarch_followup_agent) — why reps report "Persequor needs a reconnect almost daily"; the connect-card re-offer loop, revoke-on-reject, and the SF rotation race
metadata:
  type: project
---

Reviewed 2026-07-23 (read-only) in `/Users/chasengonzales/monarch_followup_agent`,
a DIFFERENT repo from grants_agent. Relevant here because Persequor is grants_agent's
approved-email handoff target ([[persequor-handoff-noop]]) — rep trust in Persequor is
a dependency of Grant's outreach flow.

Rep report (Anthony Dambrosio, 2026-07-23 08:10 PT): "do we have to reconnect everyday
for persequor?" — 40 min after the 07:30 PT morning cron.

**Fact (code-verified):** `salesforce_onboarding.ensure_connect_prompts` runs at EVERY
process start (`backend/main.py:76`) and daily on the morning cron
(`backend/jobs/morning_scan_cron.py:39-62`). Its only stop condition is
`salesforce_connection.status == 'active'`; its only throttle is a 24h
`salesforce_connect_prompt.sent_at` window. Inside that loop, a rep whose stored
`google_account.scopes` is not a superset of `google_oauth.SCOPES` receives a
**"Reconnect Google"** card — with NO per-scope-set nag guard, unlike
`onboarding.ensure_scope_current`, which sends exactly one card per scope-set. Two
passes, same condition, opposite nagging policy.

**Fact (code-verified):** `onboarding._complete_connection` tests scopes with a
substring `in` against the raw scope string; `ensure_connect_prompts` and
`salesforce_onboarding.handle_callback` test exact-token `issubset`. The write gate is
LOOSER than the read gate, so a stored value can be permanently un-satisfiable.

**Fact (code-verified):** `onboarding.handle_callback._reject` revokes the just-issued
token on ANY rejection (partial scope, wrong account, unexpected error). Whether Google
revocation is grant-scoped (killing the rep's PREVIOUS working refresh token) is NOT
verifiable from the repo — assumed, needs a controlled test.

**Fact (code-verified):** `salesforce_oauth.access_for_rep` has a rotation race — 8
callers across the APScheduler thread and the Bolt thread read one stored refresh token;
under Salesforce refresh-token rotation the loser gets `invalid_grant` and the code
marks the whole connection `status='revoked'`. It also treats `invalid_client` (a bad
CLIENT SECRET — a config fault) as a per-rep revocation, which would tell every rep to
reconnect for a fault no rep can fix.

**Lesson to carry into grants_agent:** an "offer until satisfied" loop needs a
satisfiability proof and a cap. Any nag whose stop condition depends on an external
platform returning an exact value must be paired with (1) one write gate and one read
gate that use the SAME comparison, and (2) an escape hatch that reports "I cannot
satisfy this" to a human instead of re-asking forever.
