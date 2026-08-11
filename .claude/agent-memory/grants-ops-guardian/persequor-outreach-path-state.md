---
name: persequor-outreach-path-state
description: The Persequor outreach path HAS worked in prod (7 accepted briefs, 2026-07-15..18) — 'submitted' means a real HTTP 2xx from the intake; but the rich-card button half is dead three ways and one real click was swallowed as Unhandled
metadata:
  type: project
---

Read-only audit 2026-08-10 at `1ffe7ce`, schema 39, listener PID 60352.

## `outreach` semantics — do NOT read `submitted` as "written locally"

`persequor_client._attempt_saved` sets `status='submitted'` **only** inside
`if resp.status_code in (200, 201, 202)` after a real
`POST {PERSEQUOR_API_URL}/api/v1/outreach-request` with `X-Persequor-Key`.
`campaign/actions.py:259` maps it to `persequor_intake_accepted`. So a `submitted` row is
positive evidence the intake accepted the brief. The full vocabulary:
`draft` → `queued` → `sending` → `submitted` | `rejected` (4xx, terminal) | `failed`
(retry limit) — and `unreachable` is a *return state*, persisted as `queued`.

**Prod has 8 rows: 7 `submitted`, 1 `draft`. All 7 dated 2026-07-15..18, `last_error` NULL,
`request_id` set, `attempts` 1.** Nothing since. `sent_at` and `response` are NULL on every
row and `approved_by` is NULL — that is **migration 468-471 deliberately** moving
`sent_at`→`submitted_at` and nulling `approved_by`, not a missing approval. Grant's handoff
ends at intake acceptance; **nothing in `grant_watch` ever writes `sent_at` or `response`**
(grep: only the migration touches them), so the DB can never tell you an email actually went
out. Do not claim delivery from this table.

**No `outreach-retry` cron line exists** (25-line crontab characterized this session), so a
`queued` row would sit forever. Currently 0 queued, so it is latent, not active.

## The two paths differ — say which one you mean

- **Conversational (`draft_email` intent, `grant.py:600-665`) — PROVEN, this is the one that
  produced all 7.** Gates: caller must be in `config/reps.json` (6 reps; `rep_email_for`
  returns None → refusal), then a `verified` contact or `OUTREACH_TEST_EMAIL`, else it
  refuses rather than guessing. On a dark endpoint it falls back to a copyable draft.
- **Rich-card "Ask Persequor to draft" button — DEAD THREE WAYS**, all still true at
  `1ffe7ce`: (1) `card.py` only appends the button `if not research`, and every card is
  `card_mode='research_needed'`; (2) that mode is unreachable because `leads.nces_website`
  is **0 of 10721** and has **no writer**; (3) even if clicked, `SLACK_WORKSPACE_ID` is
  **still absent** from `.env` and from PID 60352's environ, so `_authorized_snapshot`
  raises `PermissionError` on its first gate. `rich_card_actions` = **0 rows**.

## A real click WAS swallowed — `bot.log:558`

```
Unhandled request ({'type': 'block_actions', 'block_id': 'BWMjD', 'action_id': 'rich_persequor_draft'})
```
The only `Unhandled request` line in the whole log. It predates
`slack/proactive_actions.py` (mtime 2026-07-25), which registers the handler and IS wired
(`grant.py:201-203`). So a human pressed that button on a build with no listener and got
**nothing at all** — not even a refusal. This is why "`rich_card_actions` is 0" must never
be read as "nobody ever clicked": a click before the handler existed leaves no DB trace,
only this log line. Extends [[slack-workspace-id-missing]] — that memory was right about the
gate but this proves someone actually tried.

## Port 8002

`ss -ltn` shows `LISTEN 127.0.0.1:8002` (also 8000/8001/8003/8090/8091/3000). Process
attribution needs root and the listener is **not** grantwatch's, so it stays unattributed —
never `sudo` to identify it, and never POST to probe it.

Related: [[tenant-and-layout]], [[readonly-db-forensics-recipe]].
