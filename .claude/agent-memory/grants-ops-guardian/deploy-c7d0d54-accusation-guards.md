---
name: deploy-c7d0d54-accusation-guards
description: CURRENT PROD 2026-08-10 — 43e6f1d (c7d0d54 + opt-out routing fix), schema 39, PID 67672; four false-accusation paths closed; manager IS in C01DGT9D11D so the membership guard suppresses nothing
metadata:
  type: project
---

## FINAL STATE: `f7cff1d3d75d89dd9c65d5e02c82d77e1873f38a`, PID 68476 (2026-08-10 ~21:12 PT)

Sixth and last deploy. 14 files (13 mods + 1 add `docs/status_log_archive.md`), 8 commits,
schema 39, `.env`/crontab byte-identical, 0 new tracebacks, `followup_nudges` still 26.

**A COUNT I GOT WRONG, AND IT PROPAGATED.** I reported "13 of 19 distinct undeclared slugs
have no wording". The real figure at `43e6f1d` was **16 of 19**. The coordinator adopted my
13 verbatim, put it in his next instruction AND in CLAUDE.md, and described the follow-up
deploy as "the 13 unwritten capability slugs are written". **A number I state casually becomes
someone else's premise within one message.** State counts as measured-or-not, and re-measure
before anyone builds on them.

**Measured on the deployed `f7cff1d` bytes** (`wording_exists()` per slug, against the live
`capability_asks` table):

```
UNDECLARED open-ask slugs : 19
NOW WRITTEN               : 6
STILL MISSING A WORDING   : 13   <-- coincidentally 13 again, a DIFFERENT set
```

Only **3** slugs in the ask set gained wordings: `contact_phone_mobile_enrichment`,
`bulk_contact_enrichment`, `salesforce_lookup`. Still unwritten: `add_campaign_members_via_ids`,
`campaign_member_enrichment`, `contact_lookup`, `direct_lead_field_edit`,
`filter_by_application_status`, `filter_by_award_date`, `format_spreadsheet_for_dataloader`,
`format_spreadsheet_for_upload`, `pull_lead_ids_for_campaign`, `salesforce_batch_upload`,
`salesforce_campaign_add`, `salesforce_upload`, `search_scoping`. **The export family was
described as done and is not.**

**So the "don't run `mark_available`" constraint does not lift — it CHANGES CHARACTER**, which
is better. The guard is deployed and proven behaviorally (calling
`mark_available(conn, "track_applications")` raises `ValueError: … has no hand-written
follow-up wording`, and it raises BEFORE any write, so a read-only connection is a safe way to
test it). Those 13 slugs now fail LOUDLY at declare time instead of broadcasting
"Good news — I can do that one now" to every waiting ask. The danger is gone; the capability
is not there. `track_applications` stays deliberately unwritten — `wording_exists` False by
design, because Grant once falsely promised a rep it would "keep watching these states".

**`MIN_GAP` 4h → 2h changed the DRAWN SLOTS**, as predicted: Tue 2026-08-11 for `C01DGT9D11D`
went `08:48 / 13:19` → **`09:06 / 12:08`**. Ordering held: North Palos `card_unengaged` id=34
still position **0**, `target_slack` `''` (mentions nobody), due Tue 10:30, reachable Tuesday.
Never carry slot times across a `MIN_GAP` change — re-draw them.

Checked and clean: no wording promises a direct phone line (the only `direct` hits in
`nudge_messages.py` are a CODE COMMENT explaining that `directPhone` is unlicensed on this
ZoomInfo plan while search still reports `has_direct_phone`).

## PREVIOUS STATE: `43e6f1d6ec609d22e78ca1ef92f63e42c3097b85`, PID 67672

Fifth and last deploy of 2026-08-10 (~20:35 PT), 3 files, schema 39, `.env`/crontab
byte-identical, 0 new tracebacks, `followup_nudges` still 26. Everything below about
`c7d0d54` still holds; this adds one fix on top.

**AN OPTED-OUT TERRITORY OWNER FROZE A CARD FOREVER, SILENTLY.** `drip.py` drops the routing
mention when the owner has opted out (the card still posts — the lead belongs to the channel),
but `_unengaged_cards` recomputed `tagged` from territory WITHOUT that filter. `card_unengaged`
then suppressed as `opted_out`, which is TRANSIENT and writes no ledger row, and
`_escalation_is_premature` waited forever for a `card_unengaged` row that could never exist.
Both subjects sat due and undeliverable until they aged out — **no error, no suppression row,
no log line.** Fix: `if tagged and reminders.is_opted_out(conn, tagged, scope="nudges"):
tagged = ""`.

The durable shape: **a TRANSIENT suppression on a subject whose successor waits for the
subject's ledger row is a permanent silent stall.** When adding a suppression reason, ask what
downstream is waiting on the row it declines to write. Only PERMANENT reasons write a row.

**`followup_optouts` state (checked before AND after): 1 row —
`U01DPJVURHU`, scope `all`, `active=0`. `ACTIVE=1` count is ZERO.** So nobody is opted out and
this fix changes no current tagging; it is latent correctness. Re-check this table before
attributing any future tagging change to code.

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
