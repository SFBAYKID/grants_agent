---
name: session-final-2f1ff77
description: Final deploy 78000cf→2f1ff77 (CURRENT PROD, 2026-08-10) — 17 files, TOOL_SCHEMAS 25, the negation-aware evidence guard and the per-turn spend key both verified; user_memory is EMPTY so the broken guard never wrote a claim about anyone
metadata:
  type: project
---

**LIVE 2026-08-10T22:31:59Z.** `78000cf` → `2f1ff777d936b3cda59bcb70d3bdb329a77d27dc`
(carries `0a415fb`, `7aaca3d`, `2f1ff77`). Listener **58929 → 60352**, **0.140 s outage**.
Schema **39**, no migration. **17 files, all modifications**, 0 adds, 0 deletions.
Closure **123/123**, import closure 120/120, `.env` + crontab byte-identical, 0 tracebacks.
**`TOOL_SCHEMAS` 25** — `memory_recall` and `memory_forget` present.
Rollback tar member count asserted 17==17 before proceeding.

## The four checks

**1. The negation guard is real, and it does not reject everything.** Verified on the
deployed bytes. Refuses `"want you to email me"` AND `"email me the weekly list"` drawn
from *"I don't want you to email me the weekly list"* — both are literal substrings, both
invert her meaning once quoted alone. Accepts ordinary quotes from non-negated sentences
(`"only work Texas"`, `"I hate spreadsheets"`, `"my kid plays lacrosse"`, and
`"send me the weekly list"` from a sentence that actually asks for it).

**My first probe scored one of those a failure and it was the probe that was wrong** — I
expected `"email me the weekly list"` to pass because it is contiguous, without noticing it
sits inside the negated span. Refusing it is the entire point of the fix. Always draw a
positive control from a sentence with **no** negation in it.

**2. The per-turn spend key.** It lives in `slack/conversation.py:546`, not `slack/tools.py`
— my probe looked in the wrong module and got an `AttributeError` that read like a missing
guard. Correct results:
```
zoominfo_fill_many {confirm: True}                  -> 'zoominfo_fill_many:confirm'
  + lead_ids [1,2,3]                                -> 'zoominfo_fill_many:confirm'
  + lead_ids [9,9,9,9,9]                            -> 'zoominfo_fill_many:confirm'   INVARIANT
zoominfo_fill_many {confirm: False}                 -> ''   (pricing stays repeatable)
zoominfo_enrich_contacts                            -> 'zoominfo_enrich_contacts'
search_leads                                        -> ''   (ordinary tools unaffected)
```
Varying `lead_ids` across six tool turns no longer defeats it.

**3. Watchdog after the restart:** fires normally, 98 runs, every one `watchdog: nothing
stuck`. **No purge line has ever appeared** — consistent with `user_memory` being empty
(nothing to purge), but it means the purge half is `needs-testing`, not verified. The first
row that expires is the first real exercise of that path.

**4. `user_memory` is EMPTY — 0 rows.** This is the answer that matters: `capture` shipped
at 76473e5 (~06:51Z) and ran all day against real traffic with a guard that accepted
meaning-inverted quotes, and it **wrote nothing about anybody**. There are no claims about
named colleagues to review or delete. The `MIN_WORTH_READING = 60` threshold plus the
`app_id` gap plus ordinary volume kept it at zero.

## Closing state, 2026-08-10 15:43 PT

Every cron fired today: drip 548, poll 10,379, rich prepare 3, nces bind 13, remind 16,
nudge 28, announce 1, scan-threads 1, watchdog 98.
`followup_nudges` **26 — 2 delivered** (Kerry 10:00, Jocelyn 14:15), 24 suppressed stale.
`announcements` posted 08:00. `capability_asks` **34, 5 armed / 29 unarmed** — unchanged;
**nothing was armed this session**. `contacts` 97. ZoomInfo **14 of 1000**. Receipts 416
complete + **2 permanently `processing`**. Schema 39, integrity ok, FK exactly the two
approved orphans. `.env` `9b68bc18…c634`, crontab `34002d4b…7ab5` (25 lines, 10 active).
Disk **71%** (68% at session start). 40 credential copies still HELD; retention plan still
UNAUTHORISED.

Related: [[closing-pass-78000cf]], [[kerry-email-sent-and-the-15-row-cap]],
[[email-results-cannot-send-a-long-list]].
