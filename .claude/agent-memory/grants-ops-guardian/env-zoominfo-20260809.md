---
name: env-zoominfo-20260809
description: ZoomInfo creds on tenant .env — durable CLIENT_ID+CLIENT_SECRET replaced the dead 24h access token 2026-08-09; still inert (no code reads them); CURRENT baselines .env sha 5cb3d3b1…/57 lines, crontab 10 lines 575fbc7c…, listener PID 1227
metadata:
  type: project
---

Two Chase-authorized edits to `~/grants_agent/.env` on the grants tenant, same day (scoped SSH,
stdin delivery so no secret ever hit argv or shell history).

**Op 1, 2026-08-09T18:30Z** — appended `ZOOMINFO_CLIENT_ID` + `ZOOMINFO_ACCESS_TOKEN`.
**Op 2, 2026-08-09T19:05Z** — SUPERSEDED op 1's trailing block: 3 comment lines → 7, CLIENT_ID
retained, `ZOOMINFO_ACCESS_TOKEN` DELETED, `ZOOMINFO_CLIENT_SECRET` added. Chase got the durable
client secret from the ZoomInfo DevPortal, so the Okta access token (24h life, expired 2026-08-10)
is gone rather than left as dead weight. Backup `~/.env.bak.20260809T190530Z` (600, sha == pre-image).

**Status: still INERT.** `grep -rI ZOOMINFO` over the deployed tree (excl. `.venv`/`.git`/
`__pycache__`/`.env*`) returns **ZERO** files, re-verified after op 2. Nothing reads these vars, so
no restart was needed or done. **ROOT CAUSE ESTABLISHED 2026-08-09: the ZoomInfo CODE was never
deployed.** `enrich/zoominfo.py`, `enrich/zoominfo_credits.py`, `enrich/zoominfo_enrichment.py` and
`migrations_zoominfo.py` exist only in the local repo (commits `e074b62`/`35492ae`/`fcb1537`, all
2026-08-09, after the 08-06 deploy of `90f0420`). So this is "credentials waiting on a deploy", not
"a feature switched off" — see [[deployed-vs-local-drift-20260809]]. Per the block's own comments the pair mints 24h bearer tokens on
demand at the Okta token endpoint, so nothing in `.env` expires any more.

**How to apply — CURRENT baselines (these supersede op 1's `11447d92…`/53-line values):**
- `.env`: sha256 `5cb3d3b158cb9356a6ea3cd1a2ab084a5902e3e7ef3648f057ca6f5e87fd9df0`, **57 lines,
  2754 bytes**, mode 600 grantwatch:grantwatch. Structure: 47 prefix lines
  (sha `b3f338ff5c42161194c6df8ee5dc1bf323dcfb0613a33a256ad034806cfc3bff`, UNCHANGED across both
  ops) + blank line 48 + 9-line ZoomInfo block starting line 49
  (sha `5c6404a942fe7ea3d12bba49d3c1c2bb24c5c30639d5528fac83da306ff1f699` = 7 comments + 2 vars).
- crontab: 10 lines, sha `575fbc7ce7c7dc24dc2e806fe15ff37c9db98808d269b31ff1eb67aed0041a72` —
  observed before AND after, untouched. Still DRIFT from the 5-line `70e309aa…` baseline of
  2026-08-06 and its 10 lines have STILL never been inspected; characterize them read-only before
  relying on any crontab assumption ([[codex-parallel-writer-forensics]]).
- Listener PID 1227, started Sun Aug 9 03:55:01 2026, `.venv/bin/python -u -m grant_watch.slack.grant`.

**LOCAL/DROPLET `.env` FILES ARE NOT COPIES OF EACH OTHER — never sync them wholesale.** The local
repo `.env` is 69 lines and its prefix genuinely differs. **CORRECTION 2026-08-09 (measured, this
said the opposite): `GRANT_SALESFORCE_WRITE_CHANNEL_IDS` is ABSENT from the droplet `.env`
entirely — 0 matching lines, and it is not among the droplet's 30 keys.** It is empty in the LOCAL
`.env` and present only in `.env.example`. That matters: `254bd5c` makes the allowlist fail CLOSED,
so setting it is a DEPLOY PREREQUISITE. `ZOOMINFO_MONTHLY_CREDITS` is likewise absent (the ledger
refuses every paid pull when unset). Only ever sync the NAMED BLOCK a task authorizes, and
prove the untouched prefix by hash. Local also carries 3 blank lines before the ZoomInfo header vs
the droplet's 1 — deliberately NOT matched, because normalizing that whitespace would mutate the
prefix and break the first-47 proof for a purely cosmetic gain.

**`pgrep -f <pattern>` SELF-MATCHES over ssh** and will invent phantom listeners: the remote
`bash -c` carries your pattern in its own argv, so `pgrep -u me -f grant_watch.slack.grant` returned
3 PIDs where 1 process existed. Confirm process identity with
`ps -u "$(whoami)" -o pid,ppid,etimes,lstart,args | grep "[g]rant_watch..."` and read `lstart`.

**Recipe for a secret-bearing block REPLACEMENT (not just an append):** stage the exact block
locally with `sed -n '/^# --- MARKER/,$p'` into a chmod-600 scratch file; assert its line count,
var count and that the retired var name is absent; back up remotely and prove backup sha == source
sha; then in ONE ssh invocation re-check the pre-image sha and the marker's line number (abort on
drift), `head -n <prefix> .env > .env.tmp`, `cat >> .env.tmp` from stdin, `chmod --reference=.env`,
and **verify `.env.tmp` before swapping** — prefix hash, block hash, var counts — `mv` only on
success, `rm` the temp otherwise. That makes the swap fail-closed: a bad staged file never becomes
`.env`. Delete the local scratch file and grep the scratchpad for residue afterwards.
