---
name: thread-scan-ratelimit-truncation
description: scan-threads silently drops most of the channel — 295 of 507 threads came back `ratelimited` into a bare `except SlackApiError: continue`, so three runs reported 29, 13 and 4 threads and every one of them read as a complete scan
metadata:
  type: project
---

**Measured 2026-08-10 on production, three runs of `scan-threads --channel C01DGT9D11D`
within 40 minutes:**

| run | threads scanned | unmet asks | skipped as not Grant's | transcripts fetched |
|---|---|---|---|---|
| 1 (dry) | 29 | 17 | 380 | 409 |
| 2 (`--execute`) | 13 | 15 | 276 | 289 |
| 3 (instrumented) | **4** | — | 208 | **212** |

The channel has **507 parent messages** in the 45-day window, every run.

## Where it goes

`fetch_threads` has two swallow points, `grant_watch/thread_scanner.py`:

```python
        except SlackApiError:
            break        # history pagination
...
        except SlackApiError:
            continue     # per-thread conversations_replies
```

The instrumented run accounted for every dropped thread:
`DROPS: {'SlackApiError:ratelimited': 295}`. One scan issues **507
`conversations.replies` calls** back to back; `conversations.replies` is a Tier-3 method,
the `WebClient` is built with **no retry handler**, so a 429 raises and the thread is
dropped with no counter, no log line and no effect on the exit code.

**The history walk is NOT the problem** — measured twice, 3 pages, 200/200/107, clean
`next_cursor=None`, no error, no `MAX_PAGES` truncation. I assumed pagination first and
was wrong; a bounded 80-call replies probe also came back 80/80 OK, which is what makes
this hard to see. It only appears at full burst.

## Why it matters

- The summary line — `scanned 13 threads … 15 unmet asks` — is **indistinguishable from a
  complete scan of a quiet channel**. The module's own docstring warns about exactly this
  at the *paging* layer ("A scan that silently sees nothing reports 'no unmet asks'") and
  then reproduces it one layer down.
- Runs get *worse* as the token's rate budget depletes, so a retry looks like a
  regression.
- `grant_watch/slack/grant.py:664` reads thread context with **the same method**, so the
  burst can degrade Grant's live replies for the ~2 minutes it runs.

## Mitigation in place (operator-side, no code change)

The weekly cron job runs **Monday 04:40 PT (07:40 ET)** — deliberately before the 08:00
local gate for every rep on the roster, so the 429 storm cannot overlap a real
conversation. `capability_asks.record` is idempotent on `(audience, message_ts,
capability)`, so a truncated week loses nothing permanently: a later run re-finds the
thread while it is still inside `LOOKBACK = 45 days`. Coverage accumulates; it just does
not arrive in one pass.

## The actual fix, when someone ships it

Give the `WebClient` a rate-limit retry handler, e.g.
`WebClient(token=…, retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=3)])`,
and **count the drops** so the summary line can say "scanned 212 of 507 threads" rather
than implying it saw everything. Both belong in `thread_scanner`/`cmd_scan_threads`, not
in cron.

Related: [[deploy-cdfdaf9-threadscan]], [[grant-slack-event-flow]],
[[firecrawl-paid-call-surface]].
