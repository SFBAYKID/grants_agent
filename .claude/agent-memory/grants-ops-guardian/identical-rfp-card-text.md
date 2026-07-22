---
name: identical-rfp-card-text
description: "Grant repeated the same RFP card" can be TWO different leads — build_rfp_alert renders only entity + keyword + due date, so sibling solicitations look byte-identical in Slack
metadata:
  type: project
---

Verified read-only on the droplet 2026-07-22 (deployed revision 15263d2, byte-confirmed).

**A repeated-looking Slack card is NOT proof of a duplicate lead.** `drip.build_rfp_alert()`
composes the card from exactly three inputs: `display_entity_name(entity_name)`, a
keyword-derived `subject` (`security cameras` / `access control` / `security cameras and
access control` / `physical security`, chosen by regex over title+evidence), and
`funds_end`. **The title is never printed.** So two genuinely distinct solicitations from
one agency with the same deadline render the SAME sentence.

That is exactly what happened: `posts` rows 18 and 20 are different leads —
- #9533 "…Control Room, Security Cameras and Other Facility Upgrades - **General and HVAC
  Construction**", due 2026-07-23, posted 2026-07-20
- #9565 "…- **Plumbing Construction \*REBID\***", due 2026-07-23, posted 2026-07-22

Both → "Pennsylvania Department of Corrections … security cameras … responses due
2026-07-23". Only three RFP posts exist in the whole `posts` table (18/19/20 on 07-20,
07-21, 07-22) and each has a distinct `lead_id`, so **no lead was posted twice** —
`rfp_candidates`' `l.id NOT IN (SELECT lead_id FROM posts)` guard held. The
[[rfp-dedup-key-drift]] duplicate really was fixed; this is a *presentation* collision.

**Why:** Chase reported "the same RFP card every morning" and named the dedup fix as the
suspect. Chasing dedup would have been the wrong hunt.

**How to apply:** when a repeat is reported, read `posts.lead_id` FIRST. Same lead_id twice
= a real dedup/guard failure. Different lead_ids = the card text lacks a distinguishing
detail (title/trade package), which is a rendering question for Chase, not a data bug.
Corollary: two sibling packages of one construction project are legitimately two leads —
eabf6e5 widened the dedup key on purpose so they would not collapse.
