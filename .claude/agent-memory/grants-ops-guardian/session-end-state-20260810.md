---
name: session-end-state-20260810
description: End-of-session production state at 750937b (2026-08-10 01:53 PT) — droplet == branch head, 10 cron jobs installed of which 5 have fired and 5 are still AHEAD of their first slot, and the proactive stack has delivered nothing yet
metadata:
  type: project
---

**Deploy LIVE 2026-08-10T08:47:53Z.** `ec1c4a4` → `750937b7e4e32723ef717fddc784fa737d7fd3d6`
(2 commits). Listener **42839 → 43889**, **0.141 s outage**. Schema **39**, no migration.
3 files, all modifications. Closure **123/123**, import closure 120/120, `.env` + crontab
byte-identical, 0 tracebacks. Backup `~/backups/deploy-750937b-20260810T084750Z/`.

`mark_lead_do_not_call(self, record_id: str)` exists and takes **only a record id** — no
caller-supplied content, exactly the narrow surface recommended. **Nothing calls it**
(grep outside tests finds only the definition), so deploying it changed no behaviour.
`fill-leads` now counts `not in this org` separately and no longer exits 1 on a clean run.

**Droplet == branch head.** `750937b..HEAD` deployable delta = **0 files**; local HEAD ==
origin == `750937b`. Branch `review/rich-award-card-campaign-20260723`, **136 commits ahead
of `main`**, and `750937b` is NOT an ancestor of main — production tracks the review branch.

## THE TIMING FACT THAT DOMINATES THE END-OF-SESSION REPORT

At session end the droplet clock read **Mon 2026-08-10 01:53 PT**. Every "first run this
morning" job was still HOURS AHEAD of its first slot — none had failed, none had been due:

| job | first slot | status at 01:53 PT |
|---|---|---|
| scan-threads | Mon 04:40 | **2h 47m away** |
| nces-bind | Mon 07:20 | **5h 27m away** |
| announce | 08:00 | **6h 07m away** |
| remind | from 08:00 | **6h 07m away** |
| nudge | slot 09:54 | **8h 01m away** |

A brief that says "several were due to run this morning — report what happened" can be
written from a wrong assumption about the hour. **Read the droplet clock before answering
any "has it fired yet" question**, and report "still before its first slot" as its own
outcome, distinct from "installed but untested" and from "ran and failed".

## Fired vs never-fired at session end

**FIRED:** keepalive (7,223 lines), watchdog (15 runs), drip (524 lines, last Fri 08-07),
poll (10,373 `NEW WATCH/GOLD/SILVER` lines; newest `source_observations` Fri 08-07),
rich-prepare (2 runs).
**NEVER FIRED:** nces-bind, remind, nudge, announce, scan-threads — all five ahead of
their first slot.

**The anchored-grep trap, third time this session:** poll's output lines carry LEADING
WHITESPACE, so `grep -c '^NEW '` returns **2** while `grep -cE 'NEW (WATCH|GOLD|SILVER)'`
returns **10,373**. An anchored pattern is a claim about column 1. Corroborate every count
against the table the job writes.

## Proactive stack: nothing delivered yet

- `followup_nudges` **0 rows** — Grant has still never sent a proactive follow-up.
- `announcements` 1 row, `posted_at=None`, `slack_ts=None` — not posted.
- `reminders` 1 row, `cancelled` (Chase's own test).
- `user_memory` **0 rows** — no real message has arrived since the feature shipped.
- `capability_asks` 20 rows: **5 armed, 15 unarmed**.
- watchdog has repaired exactly **1** receipt (Ev0BP34QSCN6, closed as already-answered at
  boot 06:31:09Z); every tick since reports `nothing stuck`. Two receipts remain
  `state='processing'` because `_mark_reviewed` moves `reviewed_at`, never `state`.

## Data state

`contacts` 97 rows — mobile **4**, email **33**, phone 16. ZoomInfo **14 of 1000** credits
consumed this period. Schema 39, `integrity_check` ok, FK exactly the two approved orphans.
`.env` `9b68bc18…c634`, 33 keys, 0 empty. crontab `34002d4b…7ab5`, 25 lines, 10 active.
Disk 68%, 16 G. `~/backups` grew with each deploy and the retention plan remains
**unauthorised** ([[backups-retention]]); 40 credential copies remain **held**
([[env-credential-sprawl]]).

Related: [[dnc-retroactive-marking]], [[lead-fill-provenance-and-cron-firing]],
[[salesforce-lead-fill-executed]], [[contact-fill-first-bulk-buy]].
