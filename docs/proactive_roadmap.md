# Proactive prompts — roadmap of what Grant COULD say (proposals)

Chase's copilot vision: Grant shouldn't just answer — it should *notice* things and
offer the next action, unprompted. Today Grant proactively posts only the bare award
nugget, the program bulletin, and (built, unscheduled) the follow-up nudge. This is a
menu of RICHER proactive prompts to build, each labeled with status and rough effort.
Nothing here is built yet — these are proposals for Chase to pick from.

Every proposal keeps the honesty invariants: real evidence only, a source link on
every funding claim, human approval before any write/email, no fabricated contacts.

---

## A. Upgrade the award nugget into an actionable card  `proposed · medium`
Today: `Commerce ISD in TX has a verified $500,000 SVPP funding award. Source: …`
Proposed: chain discovery → contact → offer, in one card:
> **Peoria Unified School District (AZ)** just landed a verified **$500,000 SVPP**
> award (spend window 2025–2028).
> The likely contact is **Jane Doe, Director of Technology** (jdoe@peoriaud.org).
> Want me to add her to Salesforce, or draft an intro about cameras & access control?
> *Source: usaspending.gov/award/…*

- Runs contact enrichment when a nugget is picked (bounded, cached).
- If no verified contact: "I couldn't confirm a contact yet — want me to dig?"
- This is the single highest-value upgrade and matches Chase's example verbatim.

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
1. **A** (actionable nugget) — the core of the copilot feel, Chase's own example.
2. **E** (backlog digest) — unlocks 855 stranded leads honestly.
3. **F** (schedule the follow-up nudge) — already built, just wire the cron.
4. **C/D** (deadline watch + weekly digest) — recurring proactive value.
5. **B/G/H** — refinements once A/E/F prove out.

Each ships behind the same gates: human approval before any write or email, a source
on every claim, honest "not found" over a guess.
