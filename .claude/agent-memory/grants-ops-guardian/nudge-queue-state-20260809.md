---
name: nudge-queue-state-20260809
description: First look at the live nudge queue (2026-08-09) — 36 due candidates but 28 suppressed 'stale'; DROP_AFTER=5d means the backlog is permanently unreachable and only a 3-day window is ever sendable
metadata:
  type: project
---

First read of the production nudge queue, read-only, right after [[deploy-beb0520-nudge-force]].
`followup_nudges` was **0 rows** — no nudge has ever been sent, so nothing was blocked by the
one-shot rule. Every observation below is `verified` from a `connect_readonly()` walk using the real
`nudges.candidates` / `suppress_reason` / `pacing_reason` functions.

**The shape that matters: 36 due candidates, and `run()` rejected the first 28 as `stale`.**
`GRACE` makes a subject due after 1h–2d, but `DROP_AFTER = 5 days` from `stalled_at` suppresses it.
So a subject is only ever sendable inside roughly a **3-day window**, and at 1 nudge/day (cap 2,
`MIN_GAP` 4h) a backlog that accumulated while the feature was off can **never** be drained — those
28 are dead forever, silently, and each future `run()` re-walks and re-rejects all of them. Grant
also never posted them, so nothing is lost from the team's view; but if the intent is ever to work
the backlog, `DROP_AFTER` is the knob, not the caps.

**`--force` is what exposed this at all.** `in_window` requires a weekday, so on a Saturday the
un-forced dry run returns `skip: outside business hours` **before** any candidate is evaluated —
the queue is invisible. With `--force` the same command names a subject. Useful diagnostic property:
`nudge --dry-run --force` is a read-only queue inspector, safe to run any day.

**Verified inert:** `--dry-run` uses `db.connect_readonly()` and returns before `_record`. Across
both dry runs the DB mtime (1786314229), size (26,828,800) and `-wal` were byte-identical and
`followup_nudges` stayed 0. Proven by mtime, not by exit code.

**Audience splits evenly, but every ELIGIBLE subject is PRODUCTION.** The 36 due candidates are
**18 `C01DGT9D11D`** (production, real people) / **18 `C0B02721MNK`** (playground) — but all 8
eligible ones are production; **all 18 playground candidates are suppressed `stale`**, because
playground traffic stopped being generated more than 5 days ago. So there is currently **no way to
exercise a nudge in the playground** without first creating fresh playground activity and waiting out
`GRACE` (2d for `card_unengaged`, 1d for `crm_batch_blocked`, 1h for `crm_preview_expired`).
CORRECTION: the first version of this memory claimed all 36 were production — I generalized from the
tail of a walk that had stopped at the chosen candidate instead of counting. The distribution above
is measured.

Underlying volume for reference: `posts` 16 prod / 17 playground; `crm_actions` 18 prod / 31
playground; `crm_campaign_batches` 2 prod / **0** playground (so batch subjects can never arise there).

**`card_unengaged` mentions nobody.** `target_slack=''` by design ("a card belongs to the channel,
not one person"), so `build_message` emits no `<@…>` and no phone notification fires; the nudge is a
**threaded reply** on the original card's `ts`. The `crm_*` kinds DO carry `requested_by` and DO
@-mention. So "who gets pinged" depends entirely on subject kind — check it before sending.

**A `card_unengaged` subject_id is a `posts.id`**, not a lead id; `crm_preview_expired` is a
`crm_actions.id` (uuid) and `crm_batch_*` a `crm_campaign_batches.id`.

**THE "IT WON'T PING A PERSON" GUARANTEE IS TIME-LIMITED — re-check it before every forced run.**
`candidates()` sorts by `stalled_at` oldest-first (verified monotonic) and `run()` takes the first
non-suppressed one, so ordering is deterministic, not random. On 2026-08-09 that first one was the
channel-only Coconino card (`target_slack=''`, no mention). But it goes `stale` at
**2026-08-10T18:00:02Z**, and the next in line is a `crm_batch_blocked` that **@-mentions
`U04ASV42UJD`** — 4 of the 8 eligible subjects mention that same single user id (not any of the three
known territory reps). So "a forced run cannot ping anyone" is true only until the current head
expires; the safe check is to read `target_slack` of eligible #0 from a `--dry-run --force` walk each
time, never to rely on the previous answer.

Also worth knowing: the whole `card_unengaged` feed comes from a `posts … LIMIT 60` scan for rows
with no `engagement` row, so engagement in Slack that Grant never recorded (a reaction it did not
subscribe to, a reply in another thread) reads as "unengaged". The message wording is careful about
exactly this — it says nothing has come back *here* — which is the right call given
[[grant-slack-event-flow]].
