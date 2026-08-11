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
