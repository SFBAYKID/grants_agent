---
name: deploy-c7d0d54-accusation-guards
description: CURRENT PROD 2026-08-10 — c7d0d54, schema 39, PID 67420; four reproduced false-accusation paths closed; manager IS in C01DGT9D11D so the membership guard suppresses nothing
metadata:
  type: project
---

**PRODUCTION IS `c7d0d544f7190e0e0b1b02bf683351a8cb620381`, schema 39, listener PID 67420.**
Deployed 2026-08-10 ~20:30 PT. 11 deployable files (9 mods + 2 adds: `tests/nudge_helpers.py`,
`tests/test_nudge_evidence.py`). `.env` sha `9b68bc18…` / crontab sha `34002d4b…`
byte-identical. 0 new tracebacks. `followup_nudges` untouched at 26.

Shipped ahead of the 08:00 cron because the deployed code could post **"X has not come back
to me" about a colleague who HAD answered**. That risk dwarfs a measured ~2.5s restart, so the
earlier "stop churning production tonight" advice was correctly overridden — **that advice was
conditional on there being no open question needing production, not a blanket freeze.** State
the condition when giving such advice so it can be re-evaluated rather than treated as a veto.

## The four false-accusation paths (all reproduced before fixing)

1. `_is_human` rejected ANY message carrying a `subtype` — and Slack attaches one to
   `file_share`, which is exactly what "here's the list you asked for" IS. Also
   `thread_broadcast`, `me_message`.
2. A thread longer than one page reported **VERIFIED SILENCE**: `has_more` was ignored and
   Slack returns replies oldest-first, so the truncated tail is precisely where an answer
   would be. Threshold 201 messages.
3. Reactions were invisible, though `grant.py` calls one "the cheapest +1 there is".
4. Wording claimed Grant's whole inbox while the check reads one thread.

Root cause, worth carrying: **the check inherited the LISTENER's blind spot instead of
correcting for it**, which defeats the entire point of asking Slack rather than the receipts
table. When one component exists to second-guess another, it must not reuse its filters.

## MANAGER MEMBERSHIP — MEASURED, and it suppresses NOTHING

Checked explicitly via `conversations.members` (read-only) rather than assumed:

```
C01DGT9D11D member count : 12
U01DFJWQQJ3 (manager)    : MEMBER = True
U04ASV42UJD, U01E908206M, U06RXJKRXSR, U01DPJVURHU : all True
```

So the new "manager cannot see that channel" guard is a **latent safety net, not an active
suppressor** — escalations proceed normally. Re-check with this recipe if escalations ever go
quiet unexpectedly; a silent queue could be a membership change rather than a code bug.

## `presentation.py` in the delta was PURELY ADDITIVE

It only adds `defuse_mentions()` (renders `<!here>` → `@here`, `<@U…>` → `@someone`), so its
17 importers were unaffected. Verified by importing `drip`, `search`, `tools`, `campaign.card`
before the restart, and by an empty unrelated boot log. **When a widely-imported module appears
in a nudge-only delta, diff it before assuming blast radius** — additive-only is provable.

## QUEUE AFTER THE TIER FIX (56024e9, shipped in this range)

`card_tier` now reads `leads.lead_grade`, with `style` consulted only when it names a real
rank. Hoxie went `award-brief`/rank-9 → `gold`. Order is unchanged where it matters:

```
[0] card_unengaged  34  gold $500,000  <EMPTY>       due Tue 10:30  NORTH PALOS  <- reachable Tue
[1] card_escalated  34  gold $500,000  U01DFJWQQJ3   due Tue 16:30  (Wed - past 14:45 last tick)
[2] capability_now_available 4          U04ASV42UJD  due Sun        <- reachable Tue
...
[5] offer_unanswered                    U01DFJWQQJ3  due Tue 16:15  (Wed)
[6] card_unengaged  32  gold $500,000  <EMPTY>       HOXIE
```

Tuesday's two slots should therefore go to **North Palos `card_unengaged` + `capability_now_available`
id=4** — the card is rescued without costing the oldest waiting person their turn.

`nudge --dry-run` now REPORTS the hold instead of obeying it, so it shows a real candidate:
`[dry-run] would nudge card_unengaged (a) [held: outside business hours]: Anyone want Hoxie
School District No 46? $500,000…`. Still writes nothing (db sha identical, 26 rows).
