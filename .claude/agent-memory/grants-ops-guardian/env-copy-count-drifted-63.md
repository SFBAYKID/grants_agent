---
name: env-copy-count-drifted-63
description: The droplet .env copy baseline moved 64 -> 63 between the 58b3e24 and 87d4e00 deploys with no deploy accounting for it - today's baseline is 63, and the invariant to police is INCREASE
metadata:
  type: project
---

The `find ~ -name ".env*" -type f | wc -l` baseline on the grants tenant read **64**
after the 58b3e24 deploy (2026-08-13 morning) and **63** at the start of the 87d4e00
deploy the same afternoon. No deploy step in between deleted one.

**Why it matters, and why it is not an alarm:** the invariant that protects credentials
is that the count must never **increase** — an increase means a deploy recipe wrote a new
copy of a live secret somewhere it will be forgotten (the retired `cp -a` recipe once
scattered 48 of them, see [[env-credential-sprawl]]). A *decrease* cannot leak anything.
So this is worth recording but not worth stopping a deploy over.

**LIKELIEST CAUSE, found by making the same mistake myself: the counter counts its own
bookkeeping files.** `find ~ -name ".env*"` matches far more than `.env` copies — any
filename *beginning* with `.env` matches. I created a tracking file called
`~/.envlist.87d4e00.20260813` and the count jumped **63 → 64 instantly**. Renaming it to
`~/.dotenv-inventory.…` put it back to 63. So a prior session that left a `.env`-prefixed
scratch/tracking file behind, then removed it, produces exactly the 64 → 63 drift with no
credential ever involved. This is the same self-match class as
[[pkill-f-self-match-kills-your-session]]: the measurement includes the measurer.

The parallel-writer explanation ([[codex-parallel-writer-forensics]]) is still possible
but is no longer needed to explain it, and nothing supports it here.

**How to apply:**
- Today's baseline is **63**, verified unchanged 63 → 63 across the 87d4e00 deploy.
- A sorted, path-only inventory (mode 600, **no values**) now lives at
  `~/.dotenv-inventory.87d4e00.20260813` — diff against it next time instead of comparing
  bare integers.
- **Never name a scratch/tracking file `.env*`** — it inflates its own measurement by one.
  Use a `.dotenv-*` or other non-matching prefix.
- **Capture the sorted list, not just the count**, at the start of every deploy. A count
  tells you something moved; only a list tells you *what*. I could not name the missing
  file this time because no earlier list existed.
- Treat an increase as a stop-and-investigate; treat a decrease as a note.
- 40 of these copies are **HELD deliberately** (they carry `SALESFORCE_PASSWORD` /
  `SALESFORCE_SECURITY_TOKEN` absent from today's `.env`) and were never authorised for
  deletion. Rotation was DECLINED by Chase — see [[paid-provider-authority-cutover]].
