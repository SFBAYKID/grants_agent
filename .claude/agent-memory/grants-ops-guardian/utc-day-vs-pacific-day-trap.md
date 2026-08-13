---
name: utc-day-vs-pacific-day-trap
description: Droplet timestamps are stored UTC but every operational question is Pacific — grouping by substr(ts,1,10) files a PT evening under the NEXT day, and printing %H:%M:%S without a date hides it completely
metadata:
  type: project
---

Bit me on 2026-08-12 while dating a Firecrawl burst, and it nearly produced a wrong
report.

`paid_enrichment_attempts.started_at` (and every other `*_at` column) is ISO-8601 **UTC**.
Pacific is UTC-7, so **17:00–23:59 PT lands on the NEXT UTC calendar day**.

Two compounding errors:

1. `GROUP BY substr(started_at,1,10)` groups by **UTC** day. A burst that happened on the
   evening of Aug 11 PT is reported under `2026-08-12`.
2. Converting to PT but printing `strftime('%H:%M:%S')` **without the date** then shows
   `23:06 PT`, which reads as "tonight" — even though the droplet clock said 18:14 PT and
   23:06 had not happened yet.

Together they turned "109 paid calls last night" into "109 paid calls today", which would
have pointed the investigation at the wrong cause entirely.

**The tell that saved it:** the droplet's own clock (`date`) was *earlier* than a
timestamp I had just printed as today's. Any timestamp in the future is a timezone bug,
not a discovery.

## How to apply

- Read `date '+%Y-%m-%d %H:%M:%S %Z'` off the droplet at the start of every forensic run
  and keep it visible; it is the sanity check for everything after.
- Convert to Pacific in Python (`.astimezone(PT)`) and group on the **converted** value,
  never on `substr()` of the raw UTC string.
- **Always print the full date with the time.** `%Y-%m-%d %H:%M:%S PT`, never `%H:%M:%S`.
- Cross-check any claimed burst against an independent artifact with its own clock — here
  `bot.log`'s mtime (2026-08-11 23:51:21 PT) matched the last attempt's finish
  (23:51:12 PT) and confirmed the corrected date.

Same family as the other "the number is simply wrong and nothing errors" traps:
[[row-get-wrong-column-false-null]], [[oneoff-scripts-need-load-dotenv]].
