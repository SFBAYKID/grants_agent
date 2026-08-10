---
name: row-get-wrong-column-false-null
description: `dict(sqlite3.Row).get("wrong_name")` returns None indistinguishably from a real NULL — it made me report "the capability was never declared" about a column that was fully populated
metadata:
  type: feedback
---

**Never read a DB column through `.get()` on a name you have not just printed from
`pragma table_info`.** A misspelled key returns `None`, and `None` is exactly what a
real NULL looks like — so a *typo becomes a finding*.

**Why:** 2026-08-10, auditing whether the announcement's "I'll come find you" could
actually fire, I printed `d.get("available_at")` and `d.get("nudged_at")` on
`capability_asks`. Both came back `None` for all 5 rows and I concluded the capability
nudges were inert and the announcement was overpromising. The real column is
**`available_since`**, it was populated on every row, and the whole family of nudges
was armed and about to fire on Monday. `nudged_at` does not exist at all. I had even
printed the true column list in the same output and read past it. The measurement that
caught it was behavioural — running `nudges.candidates()` and getting 5
`capability_now_available` back — not another look at the schema.

**How to apply:** in any forensic script, either index with `row["col"]` (which raises
`IndexError` on a bad name) or assert the key set first:

```python
cols = {r[1] for r in conn.execute("pragma table_info(capability_asks)")}
assert {"available_since", "state"} <= cols, sorted(cols)
```

And whenever a read-only query is about to become a *claim about behaviour*, close it
by calling the real function the behaviour depends on. This is the same rule as
"verify a config allowlist BEHAVIOURALLY" in [[deploy-mechanism]] and the same family
as [[verify-the-premise-not-the-claim]] — a `None`, like a `0`, must be judged rather
than believed. Related: [[readonly-db-forensics-recipe]], [[deploy-f894801-announce]].
