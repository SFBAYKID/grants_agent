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

### 0. Rich verified award card — feature flag ON in production 2026-08-05  (kind `rich_award`)
- **Status:** `GRANT_RICH_CARD_ENABLED=1` in production since 2026-08-05 (Chase's
  explicit instruction; the five-business-day shadow gate was waived by him the same
  day). First live render/notification still `needs-testing` until a human confirms
  the first posted card.
- **Fires only when:** a Gold verified award for an NCES-linked district has a
  precision-safe recent award date, event-owned positive finite amount, currently open
  spend window, recent completed source run, safe exact award URL, evidenced official
  site, fresh public official contact, and fresh complete CRM result. Otherwise the
  tick FALLS BACK to the restyled legacy daily card (§1–§4 content in the rich Block
  Kit layout) — a card lands every weekday; after an outage past the rich cutoff the
  fallback card may land in the afternoon, which is accepted (Chase 2026-08-05: never
  a silent day).
- **Pacing:** one card maximum per weekday, deterministic 10:00–10:45 Pacific slot,
  hard 11:30 cutoff (`pacing.HARD_CUTOFF_PT`), no urgent second card. It shares the
  cap with follow-up reminders and with the fallback daily card (both paths count
  posts AND pre-Slack reservations, so neither can double-post).
- **Card:** GOLD/PLATINUM header; exact owner mention when a rep is mapped, otherwise
  NO routing line at all (Chase 2026-08-05 — never a guessed owner, never an
  "unassigned territory" label);
  award/spend-window facts; typed Salesforce context; evidenced contact; separately
  labelled official/contact/Salesforce/award links; `Ask Persequor to draft` and
  `Not relevant` buttons. All actions resolve the immutable posted snapshot.
- **Safety:** controlled Block Kit, complete fallback text, no arbitrary unfurls, no PII
  in button values, reservation before Slack, and no retry of ambiguous sends.

The remaining messages below describe the existing flag-OFF legacy behavior.

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

### 4. Proactive follow-ups — chasing work nobody came back to  (`followup_nudges`)

Added 2026-08-11. Live on cron `*/15 8-14 * * 1-5`, delivered inside a randomly drawn
08:30–14:30 PT slot, **max 2 per channel per day, 1 per person per day**, and **one per
subject EVER** (a UNIQUE constraint, not worker logic). Two hand-written wordings per
kind; `slack/nudge_variants.py` records which was sent and whether anyone replied.

Every closing offer is **computed from the lead**, not written into the sentence —
`nudge_promises.best_offer` uses the same `contact_status='verified'` predicate the
outreach path requires, so a named offer cannot be answered by "I couldn't verify a
contact". It offers a DRAFT FOR APPROVAL and never a send: a human approves and
Persequor sends, and `outreach.sent_at` has no writer, so Grant cannot know an email
was delivered and must never say it was.

| kind | when | where | says |
|---|---|---|---|
| `crm_preview_expired` | approval lapsed +1h | thread | *"that approval timed out, so nothing got written. Want me to rebuild it? 🙂"* |
| `crm_batch_blocked` | +1d | thread | *"still stuck on {n} orgs I can't match. Want me to add the rest?"* |
| `crm_batch_partial` | +2d | thread | *"only the ones I could match went in. Want me to have another go at the rest?"* |
| `card_unengaged` | card +24h | thread | *"Anyone want {org}? {$amount}, and nothing's come back here on it. {offer}"* |
| `capability_now_available` | on ship | thread | *"{opener} you asked: “{quote}” I couldn't do it then. I can now — want me to?"* |
| `card_escalated` | card +30h | **channel** | *"heads up — {$amount} {org} went to {rep} and nothing's come back here. May be handled offline. {offer}"* |
| `offer_unanswered` | offer +26h | **channel** | *"I offered to {thing} for {person} and nothing's come back here — they first asked back on {date}. Worth a poke from you, or shall I leave it?"* |
| `thread_abandoned` | +1d | thread | *"I never got you an answer on this one. Want me to pick it back up?"* |

**The two channel kinds are the only messages Grant sends that are ABOUT one colleague
and addressed TO another**, so they carry extra guards:

- Silence is established by **reading the thread in Slack**, never from
  `slack_event_receipts` (which undercounts — a reply Grant never woke for would read
  as being ignored). `nudge_silence.replied_since` answers True / False / **None**, and
  None ("couldn't tell") is treated exactly like "they replied". An outage produces
  silence, never an accusation — and the suppression is transient, so the true claim
  survives until Slack is readable.
- A manager is never told before the rep has had their own turn: `card_escalated` is
  suppressed while no `card_unengaged` row exists for that card. Structural, because a
  cap or an outage can delay the rep's nudge past any grace period.
- An **opt-out protects the person being talked ABOUT**, not just the addressee — the
  addressee here is the manager.
- Never posted into a DM: the manager is not in it, so the message would be invisible
  while repeating private words back into a private thread.
- Wording may only report what Grant saw. "Nothing's come back **here**" and "may be
  handled offline" are load-bearing — the rep may have phoned the district from the car.

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
