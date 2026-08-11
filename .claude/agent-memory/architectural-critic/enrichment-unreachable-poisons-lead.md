---
name: enrichment-unreachable-poisons-lead
description: "REPRODUCED: a SourceUnreachable in contact enrichment writes an 'indeterminate' paid-ledger row that no code path can ever clear, so the lead reads 'error' forever — and the new 8-worker pool advertises resumability that does not exist"
metadata:
  type: project
---

# One outage permanently un-enriches a lead, and the retry copy says otherwise

`finder.find_contact` documents `SourceUnreachable` as "we never actually read a
page — the caller must record NOTHING, retryable". But it is raised from inside
`paid_calls.execute`'s `work()` callback, whose `except` clause writes
`state='indeterminate'` — the state reserved for "money may have been spent".

`contact_enrichment.enrich_lead_contact` catches `SourceUnreachable` and
`CompletedPaidCall`. It does **not** catch `IndeterminatePaidCall`. So:

```
pass 1: unreachable
pass 2: RAISED paid_calls.IndeterminatePaidCall: prior paid operation is indeterminate
pass 3: RAISED ...        (source fully recovered; finder called 1 time total, ever)
```

In `_enrich_contacts` that exception is swallowed by the pool's broad
`except Exception` and becomes a bare `error` cell. There is **no operator escape
hatch**: `retry_indeterminate=True` is wired only to `cli rich-prepare`, which uses
different request keys, so nothing can clear a `legacy-contact:{lead_id}` row short
of hand-editing SQLite.

## Why the 2026-08-11 concurrency change makes this load-bearing

- `ENRICH_WORKERS = 8` with `MAX_ENRICH_ROWS = 100` bursts up to 400 Firecrawl
  searches + 600 scrapes + 600 Anthropic calls per Slack tool call. `finder` has
  **no rate limiting, no backoff and no 429 handling anywhere** — a 429 surfaces
  only as `SourceUnreachable`, i.e. as a permanent burn.
- The change added two user-facing claims that are false for exactly these rows:
  the partial-run note ("ask again and I'll continue with the rest — the ones
  already checked are cached, so it is quick and costs nothing extra") and the
  `with_contacts` tool description ("already-checked organizations are cached, so
  the repeat continues rather than repeating").

## What the concurrency did NOT break (measured, so it is not re-litigated)

100 orgs x 8 workers, every outcome class, plus two overlapping 8-worker searches
over identical rows: order preserved by `pool.map`, zero `database is locked`, zero
lost writes, zero duplicate `request_key`s. A 6-thread barrier race on ONE lead
produced exactly **one** paid provider call — `UNIQUE(request_key, attempt_no)` is
the real guard, not the TOCTOU SELECT in `paid_calls.execute`; the losers get
`IntegrityError` (an "error" cell, no double-spend).

## Round 2 — 85bec38: fixed, and the new status leaks

**FIXED, verified.** `paid_calls.execute(provably_unspent=(SourceUnreachable,))`
files the attempt `failed`; pass 2 against a recovered source returns `verified`
for all five leads. `finder._extract` now uses `Anthropic(timeout=60.0,
max_retries=2)`; `ENRICH_TIME_BUDGET_S` went back to 420, not up to 600.

**The new `needs_operator_retry` status is not in `ContactOutcome`'s docstring
("status is exactly one of:" still lists six) and no consumer handles it:**
- `search_presentation.contact_suffix` falls through to `f" · contact: {status}"`
  and renders the raw slug to a rep — an internal identifier in a reply.
- `tools.find_contact` falls through to *"I checked their website, LinkedIn, and
  looked for a general organization mailbox — none produced a verifiable contact,
  so I've logged this one as no contact found."* Measured: **finder called 0
  times**, and no `not_found` row written. Two false assertions in one sentence.

**H2 is improved, not closed.** Measured: model turns 18 min + budget 7 min = 25
min against `STUCK_AFTER` 20 min, and ONE `find_contact` still bounds at 4x30 +
7x60 + 7x180 = **30 minutes** for a single organization — the in-flight tail the
"stop STARTING" budget does not cover.

**`_require_priced_run` binds the lead set, not the bill.** Reproduced: priced at
`max_credits=5`, confirmed at `max_credits=100` → `('priced', 5), ('SPENT', 100)`.
A restart DOES fail closed (correct), and price→confirm in one turn with no human
between is still reachable because `_single_execution_tool_key` returns `''` for
`confirm=false`.
