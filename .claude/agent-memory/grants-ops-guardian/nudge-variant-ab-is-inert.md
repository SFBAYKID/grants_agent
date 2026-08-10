---
name: nudge-variant-ab-is-inert
description: MOSTLY FIXED in prod by a718066 — six subject kinds now have two real wordings; only card_escalated and capability_now_available still emit identical text for both labels
metadata:
  type: project
---

Migration 36 and `slack/nudge_variants.py` shipped a wording experiment: every nudge
records which `variant` was sent, and `nudge-report` prints a reply rate per variant.
`choose()` hands back a label for **every** subject kind. `build_message()` only branches
on that label for **three** cases.

**Measured against the live production queue on the day it shipped** (14 eligible
subjects, `build_message(c,"a") == build_message(c,"b")` evaluated per subject):

| kind | two distinct wordings? |
|---|---|
| `card_unengaged` *with* a tagged rep | YES |
| `card_unengaged` **untagged** | **NO — identical** |
| `crm_preview_expired` | YES |
| `thread_abandoned` | YES |
| `crm_batch_blocked` | **NO — identical** |
| `crm_batch_partial` | **NO — identical** |
| `card_escalated`, `capability_now_available` | **NO — identical** |

The untagged `card_unengaged` branch is the final `return` in `build_message`, *after* the
`if mention:` block that holds the variant-b text — so when nobody was tagged there is no
variant branch at all.

**Why this matters and is not academic:** on the day the cron was armed, 23 of 39 due
subjects and the first 7 eligible ones were untagged `card_unengaged`. So the first several
days of real nudges will write rows labelled `a` and `b` carrying **the same sentence**.
`nudge-report` will then display two reply rates side by side as though they compared two
wordings, `choose()` will after `MIN_SAMPLE=8` start preferring whichever won on noise, and
`EXPLORE_EVERY` will keep feeding the "loser" — all of it measuring nothing. That is
precisely the superstition `nudge_variants.py`'s own docstring says it exists to prevent.

**How to apply:** do not read a `nudge-report` table as evidence about wording until the
subject kind in question is one of the three that really has two texts. Before quoting a
reply-rate difference, run
`build_message(candidate,"a") != build_message(candidate,"b")` for that kind. The honest
fix is either a variant-b wording for the untagged `card_unengaged` path (the common case),
or having `choose()` return a single label for kinds with one wording so the ledger cannot
imply a comparison that was never run.

Verified read-only against production at revision d664548, schema 36
([[deploy-d664548-followups-live]]). Related: [[nudge-queue-state-20260809]].

---

## STATUS 2026-08-09: mostly fixed in production by a718066

Re-measured **on the deployed bytes** (`nudges.py` sha `5f6ccda2…f399` == the a718066
blob), schema 37 — see [[deploy-a718066-mobile-phone]]. `build_message(c,"a") !=
build_message(c,"b")` is now **True** for: untagged `card_unengaged` (the common case,
and the one the whole live queue is made of), tagged `card_unengaged`,
`crm_batch_blocked`, `crm_batch_partial`, `crm_preview_expired`, `thread_abandoned`.

**Still identical for both labels: `card_escalated` and `capability_now_available`.**
They delegate to `_escalation_message` / `_capability_message`, neither of which accepts
a variant argument, so the variant is discarded before the text is built. Both are
currently unreachable in practice — `followup_nudges` is still 0 and all 5
`capability_asks` have `available_since` NULL — but the moment `capability --execute`
declares a capability, the caveat above applies again to that kind. The rest of this
note stands as the reason the fix mattered; the table is history, not current state.
