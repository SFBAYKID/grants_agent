# AGENTS.md — grants_agent agent operating agreement

This is the tool-neutral entrypoint for any coding agent working in this repository. `CLAUDE.md`
contains the project constitution and product briefing; `architectural.md` contains the implemented
system design. Read all three before changing behavior.

## Authority and read order

1. Follow the non-negotiable constitution in `CLAUDE.md`.
2. Read `architectural.md` before designing or changing a subsystem.
3. Before source work, read `docs/source_inventory/README.md`, the canonical
   `data/source_catalog/sources.csv`, `docs/FINDINGS.md`, and
   `docs/grant_lead_source_inventory.md`.
4. Read `docs/grant_agent.md` before changing Slack behavior.
5. Inspect the worktree before editing. Preserve unrelated or pre-existing changes.

If documentation disagrees with executable behavior, verify the code and tests, update the stale
document, and label the discrepancy honestly. Never change a verification label merely to make the
documents agree.

## Required working loop

- Use the project-scoped `architectural-critic` before committing to a material design.
- Use `grants-ops-guardian` exclusively for production server or production-database operations.
- Implement with fully typed functions, module headers, function docstrings, and comments for
  non-obvious parser, security, and idempotency logic.
- Add happy-path and failure-path tests. A recorded fixture proves parser behavior; it does not prove
  that a source is currently live.
- Run the health gate below before handoff.
- Remove one-time diagnostics, backfills, scratch scripts, debug output, and unused helpers before
  handoff. Reusable operational tooling must have a documented entrypoint and tests.
- Keep every file below 1000 lines, including Python, Markdown, canonical/generated CSV, fixtures,
  and configuration. Split or shard text artifacts before 800 lines when practical.
- Commit and push small increments when repository permissions allow. Never include secrets, `.env`,
  databases, local exports, or unrelated user changes.

## Health gate

Run from the repository root:

```bash
python -m pip install -r requirements-dev.txt
ruff format --check grant_watch tests
ruff check grant_watch tests
vulture grant_watch --min-confidence 80
python -m grant_watch.health
python -m pytest tests -q
python -m grant_watch.source_discovery
python -m grant_watch.source_catalog --check
python -m grant_watch.coverage_universe
```

Also check module/function documentation and annotations when adding code, review file sizes, and run
`git diff --check`. Report each result as `verified`, `assumed`, or `needs-testing`.

## Source-discovery discipline

`data/source_catalog/sources.csv` is the canonical discovery catalog. Generated access and coverage
views live in `docs/source_inventory/` and are rebuilt with `python -m grant_watch.source_catalog`.
A catalog row marked `discovered` is a research candidate, not a working poller. Runtime pollers live
one per module in `grant_watch/sources/` and require fixtures, failure tests, and a separate live check.
New Firecrawl research must also append a secret-free immutable row to
`data/source_catalog/discovery_checks.csv` and pass `python -m grant_watch.source_discovery`.
County research must update the matching Census GEOID link in
`data/source_catalog/county_source_links.csv`, regenerate the state shards with
`python -m grant_watch.coverage_universe --refresh`, and leave every untouched entity explicitly
`not_researched`.

Never infer that a portal is anonymous, free, statewide, current, or security-relevant from its name.
Keep official ownership, access behavior, integration maturity, and live-result evidence as separate
claims. Never store a credential value in the catalog; record only its environment-variable name.

## Safety boundaries

- Scheduled workers that post to Slack, submit outreach, or write an external system must support and
  honor dry-run. The Socket Mode listener has no dry-run mode; verify it with offline tests unless an
  explicit real-channel interaction is intended.
- Salesforce reads and create-only Campaign actions use separate credentials. Campaign writes remain
  disabled until explicitly approved and sandbox-verified.
- A contact remains `not_found` when public evidence is absent. Never construct or guess an email.
- The production droplet is multi-tenant. Never use admin access, another tenant, `sudo`, or root.
