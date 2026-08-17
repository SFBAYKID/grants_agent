---
name: import-smoke-assertions-can-be-wrong
description: Two pre-restart smoke assertions failed on a deploy that was byte-perfect - a removed `def` kept as an alias, and a gate imported INSIDE main(); check the sha first, then fix the check
metadata:
  type: project
---

**A FAILING PRE-RESTART ASSERTION IS NOT AUTOMATICALLY A FAILING DEPLOY. Establish which
one is wrong by the file sha, before reacting.**

Twice during the 2026-08-17 `900af52` deploy my own smoke test failed on a deploy whose
files were already proven byte-identical to the pinned commit's blobs. Both times the
CHECK was wrong. Had I trusted the assertion over the measurement I would have rolled back
a correct deploy.

## 1. A removed `def` can survive as an alias — `hasattr` cannot see the difference

[[deploy-mechanism]] rightly says the import smoke should assert **removed** symbols too,
not just new ones, because "does the new function exist" cannot distinguish a new file
from a stale one. I built that list with:

```bash
git diff <old> <new> -- grant.py | grep -E '^-(def |[A-Z_]+ =)'
```

It reported `_in_configured_channel`, `_active_human_channel_member` and `_thread_history`
as removed. They were removed **as `def`s** and immediately re-added as module-level
aliases:

```python
_in_configured_channel = venues.in_configured_channel
```

so `not hasattr(g, "_in_configured_channel")` failed on a perfectly deployed file. The
grep saw the `-def` lines and never looked at the `+` side.

**The fix is a sharper assertion, and it is strictly better than the one it replaces:**
assert **identity**, not absence.

```python
assert g._in_configured_channel is venues.in_configured_channel
```

On the OLD bytes that name is a plain function defined in `grant.py`, so the identity
check is False; on the NEW bytes it is the venues function. That distinguishes the two
files in a way `hasattr` never could, in either direction. Also assert the old `def ...`
strings are gone from the **source text**, not just the namespace.

**Rule: when a diff shows `-def foo`, always read the `+` side for `foo` before asserting
it is gone.** A refactor that splits a module characteristically keeps the old names as
re-exports precisely so callers do not change.

## 2. `runtime_configuration_issues` is NOT an attribute of `grant`

CLAUDE.md says "`grant.py:917-919` raises on any `runtime_configuration_issues()`", which
reads like `grant.runtime_configuration_issues`. It is not. The real definition is
**`grant_watch/health.py:124`**, and `grant.py` imports it **inside `main()`** (a function-
local import at line 915), so it never becomes a module attribute and
`g.runtime_configuration_issues()` raises `AttributeError`.

```python
from grant_watch.health import runtime_configuration_issues   # the callable path
issues = runtime_configuration_issues()                        # 0 == main() will not raise
```

**How to apply:** to prove production can boot, call the gate at its real home in
`health`, with `load_dotenv("/home/grantwatch/grants_agent/.env")` first (see
[[oneoff-scripts-need-load-dotenv]] — without it the gate reads a different environment
and the number is simply wrong). `git grep -n 'configuration_issues' <sha> -- '*.py'`
locates it in one call.

## 3. Put the config gate BEFORE the assertions, or an abort skips it

Both failures aborted the script before it reached the startup gate — the single most
important pre-restart check, because it is the one that decides whether the process can
come back up. Order the smoke so the **boot-critical** check runs first, or give each
assertion its own try/except so one wrong assertion cannot suppress the rest of the
report. A smoke test that stops at the first surprise tells you least exactly when
something is surprising.
