---
name: env-zoominfo-20260809
description: ZoomInfo creds on tenant .env — durable CLIENT_ID+CLIENT_SECRET replaced the dead 24h access token 2026-08-09; still inert (no code reads them); CURRENT baselines after Stage 2 .env sha f4abd546…/66 lines/32 keys, crontab 10 lines 575fbc7c…, listener PID 1227
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

**STATUS SUPERSEDED 2026-08-09T21:28Z — NO LONGER INERT.** Stage 3 shipped `fe56807`, so the
ZoomInfo code (`enrich/zoominfo*.py`, `migrations_zoominfo.py`) IS now deployed, migration 29 created
`zoominfo_credit_periods`/`zoominfo_credit_spends` (both EMPTY), and the restart put
`ZOOMINFO_MONTHLY_CREDITS=1000` + `GRANT_SALESFORCE_WRITE_CHANNEL_IDS` into the live process environ.
**Listener PID is now 12836, not 1227.** See [[deploy-fe56807-stage3]]. The paragraph below is the
PRE-Stage-3 record.

**Status at the time: still INERT.** `grep -rI ZOOMINFO` over the deployed tree (excl. `.venv`/`.git`/
`__pycache__`/`.env*`) returned **ZERO** files, re-verified after op 2. Nothing read these vars, so
no restart was needed or done. **ROOT CAUSE ESTABLISHED 2026-08-09: the ZoomInfo CODE was never
deployed.** `enrich/zoominfo.py`, `enrich/zoominfo_credits.py`, `enrich/zoominfo_enrichment.py` and
`migrations_zoominfo.py` exist only in the local repo (commits `e074b62`/`35492ae`/`fcb1537`, all
2026-08-09, after the 08-06 deploy of `90f0420`). So this is "credentials waiting on a deploy", not
"a feature switched off" — see [[deployed-vs-local-drift-20260809]]. Per the block's own comments the pair mints 24h bearer tokens on
demand at the Okta token endpoint, so nothing in `.env` expires any more.

**Op 3, 2026-08-09T21:12Z (Stage 2 of the 90f0420→3915b11 deploy)** — appended a 9-line block
(1 blank + 6 comments + 2 vars) setting the two DEPLOY PREREQUISITES below. Both values non-secret.
Backup `~/backups/env.bak.stage2-20260809T211228Z` (600, sha == pre-image). No restart: PID 1227
keeps its old environ until Stage 3, which is the intended behaviour.

**How to apply — CURRENT baselines (these supersede op 1's `11447d92…`/53-line and op 2's
`5cb3d3b1…`/57-line values):**
- `.env`: sha256 `f4abd546713728f3aaf979cbb69e9d931f60718574968a20885554ac538d2a99`, **66 lines,
  3334 bytes, 32 keys**, mode 600 grantwatch:grantwatch. Structure: 47 prefix lines
  (sha `b3f338ff5c42161194c6df8ee5dc1bf323dcfb0613a33a256ad034806cfc3bff`, UNCHANGED across all
  three ops) + blank line 48 + 9-line ZoomInfo block at 49–57
  (sha `5c6404a942fe7ea3d12bba49d3c1c2bb24c5c30639d5528fac83da306ff1f699` = 7 comments + 2 vars)
  + 9-line Stage-2 block at 58–66
  (sha `94c6ce2f0ac8df2c81d79d28de2ab1d544bea8240306cdbe17e3d2d27236cedf`). The first 57 lines still
  hash to op 2's whole-file sha `5cb3d3b1…9df0` — that identity is the cheapest proof an append
  touched nothing pre-existing, and it only holds because the file ends in `0a`. **Check the last
  byte (`tail -c 1 | od -An -tx1`) before any append**: without a trailing newline an append silently
  fuses onto the final var.
- `GRANT_SALESFORCE_WRITE_CHANNEL_IDS=C01DGT9D11D,C0B02721MNK` (prod + playground) and
  `ZOOMINFO_MONTHLY_CREDITS=1000` are now SET — the two prerequisites recorded below as absent are
  satisfied as of op 3.
- crontab: 10 lines, sha `575fbc7ce7c7dc24dc2e806fe15ff37c9db98808d269b31ff1eb67aed0041a72` —
  observed before AND after, untouched. Still DRIFT from the 5-line `70e309aa…` baseline of
  2026-08-06 and its 10 lines have STILL never been inspected; characterize them read-only before
  relying on any crontab assumption ([[codex-parallel-writer-forensics]]).
- Listener PID 1227, started Sun Aug 9 03:55:01 2026 — **STALE: replaced by PID 12836 at
  2026-08-09 14:28:06 PT by the Stage-3 restart** ([[deploy-fe56807-stage3]]).
- The crontab's 10 lines HAVE now been characterized read-only — see
  [[readonly-db-forensics-recipe]]; Stage 3 left them byte-identical (`575fbc7c…1a72`).

**A stated line-count expectation is a DERIVED claim; the block text is the AUTHORITATIVE one.**
Stage 2's instruction said "expect 57 + 8 = 65 lines" while the verbatim block it also mandated
("including the blank line") was 9 lines → 66. Trimming the blank line to satisfy the arithmetic
would have mutated authorized content to make a check pass. Correct order: append verbatim, prove
prefix-sha + block-sha + total lines, and report the corrected count. Count the leading blank line.

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
