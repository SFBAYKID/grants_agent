---
name: rich-delivery-no-resume-path
description: campaign/delivery.run attempts a stable award EXACTLY ONCE and never resumes a reservation — so any "reuse X on retry" idempotency scheme for the rich card is dead code; add per-attempt Slack steps with this in mind
metadata:
  type: project
---

`grant_watch/campaign/delivery.py::run` is strictly single-shot per stable award, not a
resumable state machine. Sequence: `reserve_daily_slot` → `reserve_notification`
(INSERT OR IGNORE on `{channel}:award:{stable_key}`, state 'sending') → `chat_postMessage`
→ finish. `reserve_notification` returns None if the delivery_key already exists, and the
`prior` check (delivery.py:150-156, `snapshot_id` in posts/notification_outbox) returns
"skip: already exists" first. `finish_notification` only accepts
delivered/unknown/rejected/unrenderable — none re-openable. There is NO sweeper that
resumes a 'sending' or 'unknown' row.

**Consequences that recur in design reviews:**
- Never-blind-retry is enforced at the AWARD level: once reserved in ANY state, that award
  is never attempted again (a duplicate is worse than a lost lead).
- A crash between `reserve_notification` and the post permanently wedges that one card
  ('sending' forever, no post, lead consumed) — accepted tradeoff, but any slow step added
  between reserve and post (e.g. an image fetch+upload) WIDENS this loss window.
- **Any "store X on the reservation, reuse it on retry" idempotency scheme is DEAD** — there
  is no retry that reaches the reuse. Seen 2026-07-23 in the official-website-image design
  (`slack_file_id` stored on the outbox "to reuse on retry"): unreachable, and implementing
  a real resume path would break never-blind-retry. If you need per-attempt Slack idempotency,
  the guarantee already exists structurally (attempted once); don't add a reuse column.
- Adding a SECOND Slack write (upload before post) creates a second ambiguous surface the
  reserve-before-Slack model assumed was singular. An optional artifact (image) must NEVER
  finalize 'unknown' with no post — that burns the whole lead for a nice-to-have. Fall back
  to the text card instead, which is safe ONLY if `files_upload_v2` without a channel posts
  no visible message (verify live before relying on it).
