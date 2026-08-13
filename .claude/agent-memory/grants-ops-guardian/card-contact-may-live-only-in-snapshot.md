---
name: card-contact-may-live-only-in-snapshot
description: 2026-08-12 — the contact named on the North Palos card exists ONLY in the frozen rich_card_snapshot; `contacts` has ZERO rows for that lead and no such person anywhere, so "we have a contact" cannot be sourced from the live table
metadata:
  type: project
---

**A contact printed on a posted card is not evidence that the contact is in the
database.** Verified read-only on production 2026-08-12.

Lead **3100 NORTH PALOS SCHOOL DISTRICT 117** (IL, gold, SVPP, $500,000, spend window
2025-10-01 → 2028-09-30), posted as `posts.id=34`, snapshot `1f859819dc6e…`:

| Source | Contact |
|---|---|
| `rich_card_snapshots` (frozen on the card) | **Sean Joyce**, Director of Technology & Communications, `sjoyce@npd117.net`, evidence URL an `npd117.net` staff page, `contact_type=named_direct`, `contact_verified_at` 2026-08-10, `contact_expires_at` 2026-09-09 |
| `contacts` table, live | **ZERO rows for lead 3100** |
| `contacts` table, anywhere | **no row named Joyce, no `npd117` address at all** |

`contacts` holds 172 rows overall, so the table is not empty — this lead specifically has
none. The card's contact came from the enrichment path at build time and was frozen into
the snapshot; it was **never persisted to `contacts`**.

**Why this matters and is not academic:** the rep on that card asked *"Yes get me a lead
plz I'll call tomorrow"*. Answering from the live table yields **nothing**. Answering
from the snapshot yields an **email and no phone** — the one field he actually asked for
does not exist for this person anywhere in the database.

The only verified phone on the lead is `org_phone = (708) 598-5500`, which is the
**district's general office line**, an org-level field — NOT a direct line for Sean
Joyce. CLAUDE.md already records the adversarial case where Grant handled exactly this
correctly by labelling such a number *"the district office line, not a direct IT
extension"*. Any message that hands over that number must carry the same label.

**How to apply:** before stating "we have a contact for X", query `contacts` for the
lead. If the answer comes from a snapshot, say so, and say what the snapshot does and
does not contain. `card_mode='research_needed'` and `route.reason='unassigned'` on this
snapshot are the honest signal that the card was posted *without* an owner and *without*
a fully researched contact — the card said so at the time.

Related: [[prod-state-0223c10-verified]], [[nces-binding-blocks-rich-card]],
[[persequor-outreach-path-state]], [[fair-order-does-not-rescue-fresh-cards]].
