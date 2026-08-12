---
name: droplet-pytest-rich-card-flag
description: The droplet pytest baseline is 1 FAILED / 1377 passed, and the failure is environmental (GRANT_RICH_CARD_ENABLED=1 in prod .env), NOT a regression — do not roll back a deploy over it
metadata:
  type: project
---

**Droplet full-suite baseline as of 2026-08-11: `1 failed, 1377 passed, 87 skipped`.**
The laptop gives `1378 passed, 87 skipped`. Same 1465 tests; exactly one diverges.

The failure is **`tests/test_cli.py::test_unresolved_cron_outcomes_return_nonzero`** →
`AttributeError: 'object' object has no attribute 'execute'` at `grant_watch/db_delivery.py:244`.

**Why:** the test monkeypatches `cli.db.connect` to a sentinel `object()` and patches
`drip.run_drip`. But `cmd_drip` branches on `rich_card_enabled()`:

- flag OFF → calls the patched `drip_mod.run_drip` → test passes
- flag ON  → calls `rich_delivery.run(client, channel, conn, …)`, which reaches
  `channel_guard(conn, …)` → `sentinel.execute` → AttributeError

`GRANT_RICH_CARD_ENABLED` is **true in the production `.env`** (the rich card has been the
path that actually posts since 2026-08-05) and absent/false on the laptop. So the test
silently assumes an environment production has not been in for months.

**Proven in BOTH directions, which is what makes it a diagnosis rather than a guess:**

```bash
# on the droplet - the failure disappears
GRANT_RICH_CARD_ENABLED=0 .venv/bin/python -m pytest tests/test_cli.py -q   # 19 passed
# on the laptop - the failure reproduces exactly
GRANT_RICH_CARD_ENABLED=1 .venv/bin/python -m pytest tests/test_cli.py -q   # 1 failed, 18 passed
```

**It is PRE-EXISTING, not deploy-caused.** The whole causal chain — `tests/test_cli.py`,
`grant_watch/cli.py`, `grant_watch/db_delivery.py`, `grant_watch/campaign/delivery.py`,
`grant_watch/campaign/__init__.py` and `.env` — was byte-identical across the
`02377ae` → `9fb6813` deploy. Note the trap: **it passes when run in isolation on the
droplet** (`pytest tests/…::test_… ` alone → 1 passed) because an earlier test in the file
leaves the env in the state it expects. Isolation is not a clean bill of health here.

**How to apply:** when a droplet suite comes back `1 failed`, check whether it is this test
before treating it as a regression — and never roll back a deploy over it. The deeper point
is worth keeping: **the production drip path has NO test coverage of its own exit-status
behaviour.** The assertion "an `unknown:` cron outcome returns non-zero" is only exercised
on the branch production does not run. Fixing it means patching the rich path too (or
parametrising the flag), which is a product decision, not a guardian one — flagged to Chase
2026-08-11, not fixed.

Related: [[prod-state-9fb6813-verified]], [[deploy-mechanism]].
