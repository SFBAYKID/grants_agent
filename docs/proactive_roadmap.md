# Proactive prompts — implemented, gated, and proposed work

Chase's copilot vision: Grant shouldn't just answer — it should *notice* things and
offer the next action, unprompted. The legacy flag-off path posts the bare award/RFP/
bulletin ladder. A rich award-card campaign is implemented locally behind
`GRANT_RICH_CARD_ENABLED=0`; it is not enabled, merged, deployed, scheduled, or shadow-
validated against production. The remaining entries are proposals.

Every proposal keeps the honesty invariants: real evidence only, a source link on
every funding claim, human approval before any write/email, no fabricated contacts.

---

## A. Upgrade the award nugget into an actionable card  `implemented locally · OFF`
Today: `Commerce ISD in TX has a verified $500,000 SVPP funding award. Source: …`
Proposed: chain discovery → contact → offer, in one card:
> **Peoria Unified School District (AZ)** has a verified **$500,000 SVPP** award
> (award date and spend window shown only at their stored precision).
> The likely contact is **Jane Doe, Director of Technology** (jdoe@peoriaud.org).
> Want me to add her to Salesforce, or draft an intro about cameras & access control?
> *Source: usaspending.gov/award/…*

- Preparation runs before the delivery window, never inside Slack send/click handling.
- The v1 card is silent unless an NCES-linked district satisfies every award, run,
  state, exact-link, contact, and CRM evidence rule. No RFP/bulletin fallback exists.
- One immutable snapshot binds Block Kit, thread answers, feedback, and Persequor.
- `rich-prepare` defaults to a no-HTTP/no-write preview; `rich-shadow` is DB-read-only.
- Production viability and presentation remain `needs-testing` in a separately approved
  five-business-day guardian-run shadow review.

## B. "Contact found" nudge  `proposed · small`
After enrichment lands a verified contact on a surfaced lead:
> Good news — I found a verified contact for **Peoria USD**: Jane Doe, Tech Director.
> Add her to Salesforce, or draft the intro email?

## C. Spend-window / deadline watch  `proposed · medium`
> Heads up: **Tuba City USD's** SVPP spend window closes in ~60 days and they're not
> in Salesforce yet. Want me to pull a contact and reach out before it lapses?

Also covers SILVER RFPs: `An access-control RFP in WA closes in 5 days — want the details?`

## D. Weekly "new in your state" digest  `proposed · small`
One scheduled post, not a stream:
> 3 new gold security awards in **Washington** this week: Castle Rock ($500K),
> … Want the full list, an Excel/Google Sheet, or contacts for the top ones?

Directly addresses the open backlog question below.

## E. Backlog surfacing digest  `proposed · small` — RESOLVES an open product gap
Backfilled awards are suppressed from the live drip (so a 2022 award is never
announced as breaking news), which strands ~855 verified gold leads. A weekly,
clearly-labeled *digest* is the honest way to work them:
> Backlog check: 12 verified gold awards in your target states we haven't surfaced.
> Top 3 by size: … Want the list or a sheet?

## F. Salesforce follow-up nudge  `built · not scheduled`
> {Entity} still needs follow-up in Salesforce.
Exists and is tested; needs a cron entry + Chase's go to schedule.

## G. Owner/duplicate alert  `proposed · medium`
When a fresh award matches an existing Salesforce account:
> New award for **X** — but they're already in Salesforce, owned by {rep}. Want me to
> loop {rep} in instead of creating a duplicate?

## H. "You looked but didn't act" re-engagement  `proposed · medium`
> You pulled up **Peoria USD** last week but didn't take an action — still interested?
> I can grab their contact or draft an intro.

---

## Recommended build order
1. **A** (actionable nugget) — run the separately approved shadow validation; keep OFF.
2. **E** (backlog digest) — unlocks 855 stranded leads honestly.
3. **F** (schedule the follow-up nudge) — already built, just wire the cron.
4. **C/D** (deadline watch + weekly digest) — recurring proactive value.
5. **B/G/H** — refinements once A/E/F prove out.

Each ships behind the same gates: human approval before any write or email, a source
on every claim, honest "not found" over a guess.
