---
name: verify-the-premise-not-the-claim
description: Chase-validated rule — re-measure "X was already fixed / X is already deployed" against the deployed bytes instead of believing the brief; he prefers the round trip to a bad CRM write
metadata:
  type: feedback
---

When a task brief asserts that a defect is **already fixed**, that a commit is **already
deployed**, or that some production property **already holds**, treat it as an unverified
premise and re-measure it on the deployed bytes before acting on it.

**Why:** on 2026-08-09 a brief told me both `fill-leads` defects were "fixed in `d050c8e`".
They were not — the fixes landed in `8976530`, which is *after* the revision then running in
production, so the LinkedIn-title path and the duplicate write target were still live. I
measured instead of believing, and Chase's own words afterwards: *"Your instinct to verify the
premise instead of the claim is the reason that did not become a bad write to a real Salesforce
record."* He issued the correction unprompted rather than letting the false claim stand. He
would rather spend a round trip than ship a write built on a wrong assumption — the same
preference the Constitution's rule 1 encodes.

This is a **confirmation**, not a correction: the extra verification step was the right call
and should not be trimmed for speed.

**How to apply:**
- Read live `.deployed_revision` before computing any delta; never trust the brief's stated base
  ([[deploy-mechanism]] records a case where the droplet was already ahead).
- For "this is fixed", check the fix's commit against the *deployed* revision
  (`git merge-base --is-ancestor <fixcommit> <deployed>`), and prefer a behavioural proof —
  run the dry run, read the actual output — over reading the diff.
- Verify the *other* half of a fix too: that it removed the bad path AND did not silently gut
  the good one. On `65f05c7` that meant confirming the Titles still offered came from
  `verified` contacts, not just that the LinkedIn one had disappeared
  ([[deploy-65f05c7-fill-leads-fix]]).
- Say plainly which claims are `verified` vs `assumed`, and name what I did not check.

Related: [[relayed-consent-is-not-consent]] (the same instinct applied to authority rather than
to facts), [[coordinator-stop-is-stop]].
