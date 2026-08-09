---
name: pycache-purge-destroys-forensics
description: A deploy's __pycache__ purge erases the bytecode-cache mtimes that would otherwise prove which modules a concurrent process imported — capture them BEFORE purging when another writer may be active
metadata:
  type: feedback
---

`__pycache__/*.pyc` mtimes are the cheapest forensic record of what a Python process imported and
when — they reconstruct how far an interrupted run actually got. **A deploy's `__pycache__` purge
destroys that record**, and the purging session's own subsequent imports then repopulate the
directory with its own timestamps, which can be misread as the other process's activity.

**Why:** 2026-08-06, Chase Ctrl-C'd a `--post` run on the droplet at ~12:55–13:01 PDT while the
b22ed55 deploy was in flight. I purged `__pycache__` at 13:01:40 PDT, then my own import-closure
test wrote 78 fresh `.pyc` files at 13:03:11–13:03:15, `nces.pyc` at 13:03:32 (the `nces-bind`
check) and `salesforce_sync.pyc` at 13:09:39 (the sync simulation). The surviving cache described
MY commands exclusively; the evidence of how far his run progressed was already gone. The DB was
the only remaining witness.

**Hypothesis retracted, with numbers.** I proposed that the deploy's `rsync -cain` checksum audit
caused the 100%-CPU spike Chase saw. Measured on the next deploy with nothing else competing:
`tar -xf` of 912 files = **0.042 s**, `rsync -cain --delete` over 912 files both sides = **0.409 s**,
total **0.457 s**, and `/proc/loadavg` was **0.26/0.84/0.86 before, during and after — unchanged**.
The repo is small and mostly text, so the audit is far too cheap to explain a sustained spike. The
real cause was a wrapped command splitting into a bare `python` REPL. (Note even that only fully
explains why the script never ran — a REPL blocked on stdin normally sits idle, not at 100%.)

**How to apply:** when another writer may be active on the tenant (Chase running something by hand,
the Codex toolchain, an overlapping session), run
`find ~/grants_agent -path "*/.venv" -prune -o -name "*.pyc" -printf "%T@ %p\n" | sort -n > /tmp/pycache_before.txt`
BEFORE the purge. The purge itself is still correct — a stale `.pyc` silently running old queue
code is the worse failure — so capture, then purge. When reading `.pyc` mtimes afterwards, always
first ask which process wrote them: an import-closure smoke test looks almost identical to a real
application run. Related: [[deploy-d66802b-card-comma]], [[codex-parallel-writer-forensics]].
