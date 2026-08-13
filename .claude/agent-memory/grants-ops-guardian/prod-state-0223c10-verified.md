---
name: prod-state-0223c10-verified
description: CURRENT PROD 2026-08-12 — 0223c10 (origin/main), schema 40 unchanged, PID 108300, 0.116s outage; the dropped-reply fix proven live both directions; deploy died mid-flight and was resumed from measured ground truth, not from the prior report
metadata:
  type: project
---

Deployed 2026-08-12 18:43–18:50 PDT, `9fb6813` → `0223c10`. Supersedes
[[prod-state-9fb6813-verified]] as the "what is running" pointer.
**Never answer "what is running" from this index; read `.deployed_revision`.**

| Fact | Value |
|---|---|
| `.deployed_revision` | `0223c102639466f4261c82f330dccdb7aebf85db` |
| Listener | PID **108300** (was 86114), single, 53 `.venv` maps |
| Outage | **0.116 s** (old proc dead in 0.040 s) |
| Schema | **40, UNCHANGED** — `migrations.py` not in the delta |
| Delta | 40 changed paths, **6 were `.claude/agent-memory/**`** ⇒ **34 deployable** (31 mods + **3 adds**) |
| `.env` | sha `9b68bc18850800e1…` — byte-identical, mtime unchanged, 0 new copies |
| Crontab | 25 lines, sha `34002d4bc67e21f5…` — byte-identical |
| Tracebacks | 13 → **13** (0 new); bot.log 1133 → 1135 (the 2 boot lines) |
| FK orphans | 2 → 2, **compared** pre/post |
| `followup_nudges` | 30 → 30 (was 28 at the previous deploy — it moves on its own, re-measure) |
| Backup | `~/backups/predeploy-0223c10-20260813T014355Z/` (37 M, copy `integrity_check` ok) |

## THE DEPLOY DIED MID-FLIGHT. RESUMING FROM THE PRIOR REPORT WOULD HAVE BEEN WRONG.

The session dropped on an API connection error between the rsync PREVIEW and the real
rsync. The coordinator correctly refused to let me resume from my own last message and
asked for ground truth first. **The measured answer was "nothing synced at all"** — but
that was not knowable without measuring, and the dangerous alternative was live:

> A PARTIAL sync where `grant.py` landed but `nudge_threads.py` did not. The RUNNING
> process is fine (old code in memory) and every health check passes — but the next
> restart, including an unattended one, dies on import, and cron keeps firing into it.

**The classification that settles it, and it must compare against BOTH revisions:** for
every file in the delta, compare the droplet sha256 against the target blob AND the
previously-deployed blob. Four buckets: `MATCHES_TARGET` / `MATCHES_DEPLOYED` /
`MATCHES_NEITHER` / `ABSENT`. Result was 0/31/0/3 — unambiguously untouched. Checking
only "does it match the target" would have shown 31 mismatches and told you nothing
about whether that was a stale tree or a half-written one.

## THE FIX, AND HOW IT WAS PROVEN

`grant.py` discarded a plain threaded reply when the thread had neither a `posts` row nor
a `slack_conversation_threads` row — which is exactly the shape of a top-level follow-up
(`CHANNEL_POST_KINDS`: `card_escalated`, `offer_unanswered`). The return sits ABOVE
`claim_slack_event`, so a dropped reply left **no receipt, no log line and no error**.

Proven live on the deployed bytes, read-only (`mode=ro`, write attempt confirmed to
raise), **both directions**:

- POSITIVE: `is_nudge_thread(conn,'C01DGT9D11D','1786560303.015219')` → **True**
- All **6** delivered nudges → True
- NEGATIVE controls, all **False**: a real posted card in the same channel that is not a
  nudge (`1786557604.499389`), a ts off by one digit, the correct ts with the wrong
  audience, empty audience, empty ts.

`is_nudge_thread` matches `slack_ts` (what Grant POSTED) and requires `state='delivered'`,
so a suppressed or reserved nudge cannot open a thread — correct, since it never reached
anybody.

**Registration happens on the REP'S first reply, not when Grant posts.** `on_message`
calls `register_conversation_thread` only after `is_nudge_thread` returns True. So
posting another message into that thread registers nothing; the deployed code is what
makes the next reply work.

## TRAPS RE-CONFIRMED THIS RUN

- **`ssh -n … < script` runs an EMPTY script and exits 0.** Hit it again on the very
  first baseline call — `-n` points stdin at `/dev/null`. Silent false pass. Drop `-n`
  whenever piping stdin.
- **zsh eats `:gr` as a history modifier**, so `git show "$TARGET:grant_watch/x.py"`
  becomes `…dbant_watch/x.py` — **even inside double quotes**. Braces fix it
  (`"${TARGET}:grant_watch/…"`), but the durable fix is to write deploy helpers as
  **bash scripts** and run `bash file.sh`.
- zsh does not word-split an unquoted `$SSH` variable holding a command; build the ssh
  invocation inline or use an array.
- `timeout` does not exist on macOS by default.
- Column names are not guessable: `followup_nudges` uses `subject_kind` (not `kind`),
  `posts` uses `ts` (not `slack_ts`), `leads` has `amount` (not `award_amount`) and no
  `contact_status`. The sqlite3 CLI errors loudly — but see
  [[row-get-wrong-column-false-null]] for the Python path that does not.

Related: [[deploy-mechanism]], [[ssh-rate-limit-and-stdin-traps]],
[[nudge-replies-are-silently-dropped]], [[card-contact-may-live-only-in-snapshot]],
[[verify-the-premise-not-the-claim]].
