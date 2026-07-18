# Grant's message catalog — everything the bot can say

Every message Grant can put into Slack, taken verbatim from the code templates
(not from memory). Split into **proactive** (Grant starts the conversation, no
human prompt) and **reactive** (Grant answers a mention or thread reply). Use
this as the map when reviewing the bot's voice and proactiveness.

Honesty invariants that apply everywhere: facts come only from stored evidence or
tool results; no fabricated names/emails/amounts; every funding claim carries its
source link; no internal identifiers or emoji in alerts; short paragraphs.

---

## PROACTIVE — Grant initiates (weekday cron, 05:00–17:30 PT)

### 1. Award nugget — the core "good news" alert  (style `award-brief`)
- **Fires when:** an unsurfaced GOLD lead has a *verified* award event
  (announced/obligated). Backfilled/imported awards are deliberately suppressed,
  so only awards caught fresh by the daily poll drip.
- **Pacing:** 30-min ticks; aim ≤2/day, hard cap 3, absolute 4; ≥90-min gap;
  random jitter; one funding event delivered at most once per channel.
- **Template:**
  `{Entity} in {State} has a verified {$amount} {Program} funding award.`
  `Source: {per-record source URL}`
- **Live example (2026-07-18):**
  `Commerce ISD in TX has a verified $500,000 SVPP funding award.`
  `Source: https://www.usaspending.gov/award/ASST_NON_...`
- **Guards:** amount must be finite + positive; entity required; single inert
  sentence (untrusted text can't inject a link); never "just received/landed"
  without a dated event; the source line is URL-hardened (drops if unsafe).

### 2. Program bulletin — an open funding window  (style `bulletin-open`)
- **Fires when:** no nugget is available AND a fresh (<14 days) federal/CA
  application window's title passes the physical-security filter and is not
  health-sector. Cap ≤1/day.
- **Template:** `{Opportunity title} is listed as open through {date}.`
  `Source: {url}`
- **Example:** `School Violence Prevention Program is listed as open through`
  `2026-08-31.  Source: https://www.grants.gov/...`

### 3. Salesforce follow-up nudge — "you haven't followed up"  *(built + tested, NOT yet on cron)*
- **Fires when:** a Grant-created Campaign Lead/Contact has no Salesforce activity
  after a business-day grace period. Deduped per member; shares the daily cap.
- **Template:** `{Entity} still needs follow-up in Salesforce.`
- **Status:** `needs-testing` — no crontab entry yet; a product decision on
  whether/when to schedule it.

> Note: the "you forgot the email / add notes / meeting notes" cards seen in the
> channel earlier are **Persequor / the Monarch website co-pilot**, not Grant.

---

## REACTIVE — Grant answers a mention or thread reply

### Conversation openers
- **Bare `@Grant`:** `Hey! What can I help you with?`
- **Working spinner:** a rotating `/ Thinking…` / `Reading their website…` /
  `Searching for the contact…` message, edited into the final answer when done.
  (Orphans from a crash are swept at boot and finalized honestly.)

### Search flow
- **Scoping question (only when the ask names no state/org/city/entity):**
  `Quick scoping question so I pull the right things: should I look everywhere or`
  `focus on one state? And do you care about a particular kind of organization —`
  `schools, cities — or everything that qualifies?`
- **Search plan (when a plan is worth confirming):**
  `Search plan: I'll look in TX for schools — with SVPP funding, gold leads only.`
  `How many do you want — top 5, top 10, or all of them?`
- **Results:** opens with a plain-words grade split
  (`Found 269 … 99 gold (award already won, ready to spend), 170 watch`), then
  bulleted rows each ending in a `verify this record` per-record source link, then
  a next-step offer (export to Excel/Google Sheet, or find contacts).
- **Zero results:** never a dead end — offers concrete relaxations with counts
  (`Nothing in June, but there are 4,463 without the date limit — want those?`).

### Contact flow (escalation chain)
- **Verified:** `Found him: {Name}, {Title} — {email}, verified directly on their site.`
- **LinkedIn + org mailbox / LinkedIn only / org mailbox only:** each stated plainly.
- **None found:** `I checked their website, LinkedIn, and looked for a general`
  `organization mailbox — none produced a verifiable contact.`

### Salesforce flow (human-approved writes)
- **Preview card** with `Confirm in Salesforce` / `Cancel` buttons, listing every
  field, the owner, the grant context, and the duplicate-check result. Nothing is
  written until the button + native confirm.
- **Write result:** `Created Salesforce Lead {Name} (id …), logged the completed`
  `Grant activity, and added a context Note.`
- **Duplicate guard:** `There's already a Salesforce contact record tied to this`
  `lead — I can't create a duplicate from here.`

### Outreach flow (Persequor, never Grant, sends)
- **Boundary:** `I don't send email directly. Want me to have Persequor draft the`
  `intro email for your review?`
- **Handoff:** `Persequor accepted the request and will prepare a new Gmail draft`
  `for your review. Nothing was sent.` (Persequor then posts its own draft card
  with Send / Edit in Gmail / Dismiss.)

### Honest refusals & clarifications
- **Award timing:** offers the verified announcement date / discovery date /
  spend-window meanings; never invents a "funds received" date.
- **Ambiguous pronoun:** `Which org did you mean by "him"?`
- **Paid discovery:** `Paid discovery runs are disabled.`

### Source-discovery status (human language)
- Summary / coverage / reviewed-sources / recent-batches, all in plain English
  ("Counties: 3,144 in total — 56 with a source link, 3,073 not yet researched").

---

## Launch asset
`assets/grant_intro_card.png` — the "Welcome to GRANT" hero card (owl logo +
tagline). Posted to the channel only when Chase decides to introduce the agent.
