---
name: closing-pass-78000cf
description: Deploy d561cbf→78000cf (CURRENT PROD) and the closing verification — the 40-credit cap is real (proven by ledger), but two of four fixes only half-landed, and aligning TOO_OLD to DROP_AFTER silently retired thread_abandoned
metadata:
  type: project
---

**LIVE 2026-08-10T22:00:42Z.** `d561cbf` → `78000cfba8a695636af8e6696e66d36d5338d807`.
Listener **58014 → 58929**, **0.145 s outage**. Schema **39**, no migration. 6 files.
Closure **123/123**, import closure 120/120, `TOOL_SCHEMAS` **23**, `.env` + crontab
byte-identical, 0 tracebacks. Rollback tar asserted at 6 members before proceeding.

## Fix 1 — VERIFIED by the ledger, not the return string

`MAX_CREDITS_PER_CALL = 40`. Called `_zoominfo_fill_many(13 leads, max_credits=997,
confirm=True)` for real against production:

```
BEFORE consumed/spend_rows/contacts = (14, 7, 97)
-> "ERROR: I cap a single bulk pull at 40 credits. Ask for 40 or fewer, or split the leads."
AFTER  consumed/spend_rows/contacts = (14, 7, 97)
```
Boundary at 41 also refused. **Nothing spent.** Checking the ledger rather than the string
is what makes this a measurement.

## Fix 2 — HALF LANDED. `AlreadySpent` yes, `BudgetExhausted` no

The deployed loop wraps `apply_for_lead` and catches **only `AlreadySpent`**
(→ `skipped_none += 1; continue`). Correct and well-commented.

**`BudgetExhausted` and `SpendIndeterminate` are NOT caught**, though the brief said
BudgetExhausted "stops the run cleanly rather than discarding what was already bought".
Both are siblings raised by `zoominfo_credits.spend`; either one propagating out of
`fill_contacts` discards the whole `FillOutcome` after earlier leads were bought and
billed — the exact defect just fixed for `AlreadySpent`. Reachable when concurrent spend
(CLI + Slack) crosses the period limit mid-loop.

My first probe reported `continues after AlreadySpent: False` — that was crude string
slicing on `inspect.getsource`, not the code. Read the block; the code was right and my
probe was wrong. Third probe-false-negative of the session.

## Fix 3 — HALF LANDED. The guard is on the minor path only

`if not event.get("app_id")` sits at the `_handle_drip_thread` call site (line 590).
The **`_converse_general` call site (line 786) is unguarded**, and `_converse_general`
never receives `event` — its signature is `(text, client, channel, thread_ts, user,
workspace, request_token)` — so it structurally cannot check `app_id` without a change.
`on_mention` routes to `_converse_general` at lines 254 and 344, which is the ORDINARY
@mention path. So an app-authored @mention in a normal thread still reaches
`user_memory.capture`.

## Fix 4 — VERIFIED, and it silently retired a follow-up kind

`watchdog.TOO_OLD` 14 d == `nudges.DROP_AFTER` 14 d. **But the answer to "does the person
get two messages" is no — the opposite happened.**

| | window |
|---|---|
| watchdog repairs AND sets `reviewed_at` | 20 min < age < 14 d, cron **144×/day** |
| `thread_abandoned` eligible | age > **1 day** grace, `reviewed_at IS NULL` |

The watchdog reaches every receipt at ~20-30 minutes and sets `reviewed_at`;
`thread_abandoned` needs that column still NULL a full day later. **The watchdog wins by
~23 h 40 m, every time, so `thread_abandoned` can now never fire.** Live: 0 candidates.

No double message — but a capability was disabled by a constant change. The watchdog
repairs the spinner (faster, better); it does **not** reopen the conversation with an
apology and a re-offer, which is what `thread_abandoned` existed to do. Product call.

## `for_chat` sweep — two genuine leaks remain

Correctly destination-aware now: the offer-a-spreadsheet branch (692) and the truncation
trailer (926/929, a `for_chat` ternary — my AST checker only walked `ast.If` and missed
the `IfExp`, so it false-negatived that one too).

**Still chat-shaped on the email path:**
1. **`search.py:702-706`** — *"…exceeds the 5000-row export safety limit. **Refine the
   search**; no incomplete file was created."* Not `for_chat`-guarded. Reachable whenever
   an emailed spec carries `export` and >5,000 rows.
2. **`lead_digest.py:58`** — *"**Nothing new in {STATE} today**, but here's the closest
   I've got:"* The broadening opener, in the EMAIL renderer, unguarded. Drip phrasing
   ("today") in an inbox. This is the string seen locally and it is still live.

## On the confirm precondition (asked)

40 alone is sufficient to prevent a catastrophic single spend — worst case 4% of the
allowance. It is **not** sufficient for the property that matters: repetition across
turns is still unbounded (6 tool turns × parallel blocks × 40), and without a recorded
pricing call the two-step approval remains a *prompt instruction* in a codebase whose
premise is that safety is mechanical. Cheap version: store the priced lead-set hash on
`confirm=false` and require a match within N minutes before honouring `confirm=true`.

## Closing state, 2026-08-10 15:04 PT

Every cron fired today: drip 547, poll (10,373 NEW lines), rich prepare 3, **nces bind 13**,
remind 15, nudge 28, announce 1, scan-threads 1, watchdog 94.
`followup_nudges` **26 rows — 2 delivered** (Kerry 10:00, Jocelyn 14:15), 24 suppressed
stale. `announcements` posted 08:00, `slack_ts` 1786374005.238569. `reminders` 1 cancelled.
**`user_memory` 0.** `capability_asks` **34 — 5 armed, 29 unarmed**. contacts 97 / mobile 4
/ email 33. ZoomInfo **14 of 1000**. Receipts 416 complete + 2 permanently `processing`.
Disk 70% (was 68%). 40 credential copies still HELD; retention plan still UNAUTHORISED.

Related: [[kerry-email-sent-and-the-15-row-cap]], [[email-results-cannot-send-a-long-list]],
[[dnc-retroactive-marking]].
