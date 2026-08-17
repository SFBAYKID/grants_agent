---
name: live-db-is-in-the-repo-dir
description: The live SQLite DB is ~/grants_agent/grant_watch.db, NOT ~/grant_watch.db - the home dir holds only backups, and the wrong path fails with a misleading "unable to open database file"
metadata:
  type: reference
---

The live tenant database is **`/home/grantwatch/grants_agent/grant_watch.db`**
(`DATABASE_URL=sqlite:///grant_watch.db`, resolved relative to the cwd `~/grants_agent`
that every cron line `cd`s into).

**Why this bites:** `/home/grantwatch/` is littered with files that *look* like the live
DB — `grant_watch.db.bak.*`, `grant_watch.db.pre46.*`, `grant_watch.db.pre47.*`,
`grant_watch.db.pre_rerearch.*`. Every backup recipe writes there, so home is the first
place you reach for. But there is **no `~/grant_watch.db`**, and opening it with
`sqlite3.connect("file:...?mode=ro", uri=True)` raises
`sqlite3.OperationalError: unable to open database file` — which reads like a permissions
or corruption problem rather than "wrong path".

**How to apply:** hardcode `/home/grantwatch/grants_agent/grant_watch.db` in read-only
forensics and backup scripts. If a DB probe fails with "unable to open database file",
check the path before suspecting anything else. Related: [[readonly-db-forensics-recipe]],
[[tenant-and-layout]].

Two other path facts learned the same way on 2026-08-13:

- The deployed-revision marker is **`~/grants_agent/.deployed_revision`**, not
  `~/.deployed_revision`. Home holds only `.deployed_revision.bak.*` copies.
- The private paid-provider ledgers live in `~/private/` —
  `firecrawl-runtime-ledger.db`, `zoominfo-credit-ledger.db` (see
  [[paid-provider-authority-cutover]]).
