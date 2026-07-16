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
python -m grant_watch.source_discovery_batch --validate
python -m grant_watch.source_catalog --check
python -m grant_watch.coverage_universe
python -m grant_watch.school_district_universe
python -m grant_watch.incorporated_place_universe
```

Also check module/function documentation and annotations when adding code, review file sizes, and run
`git diff --check`. Report each result as `verified`, `assumed`, or `needs-testing`.

The permanent core live check is intentionally outside the default health gate. Run it manually with
`GRANT_LIVE_VERIFICATION=1 python -m grant_watch.live_verification --execute-live`. It is read-only,
rejects CI, and is restricted to exact allowlisted official award and awardee-directory hosts. Never
weaken those gates or turn this check into a Slack, Salesforce, LinkedIn, database, or outreach write.

The real-model human-question acceptance matrix is also opt-in: run
`GRANT_LLM_ACCEPTANCE=1 python -m pytest tests/test_human_question_acceptance.py -q`. Its tool layer is
canned and write-free; it tests language understanding, tool selection, confirmation, truth, and
safety without treating model wording as proof that an external action occurred.

## Source-discovery discipline

`data/source_catalog/sources.csv` is the canonical discovery catalog. Generated access and coverage
views live in `docs/source_inventory/` and are rebuilt with `python -m grant_watch.source_catalog`.
A catalog row marked `discovered` is a research candidate, not a working poller. Runtime pollers live
one per module in `grant_watch/sources/` and require fixtures, failure tests, and a separate live check.
New Firecrawl research must also append a secret-free immutable row to
`data/source_catalog/discovery_checks.csv` and pass `python -m grant_watch.source_discovery`.
The batch collector stores every raw result and attempt under
`data/source_catalog/firecrawl_batches/<batch_id>/`; validate that evidence with
`python -m grant_watch.source_discovery_batch --validate`. Raw batches are research evidence only:
they must never update the catalog, entity links, discovery checks, or runtime pollers automatically.
Promotion requires a human to review the official page, verify the access boundary, and record the
selected-result and scrape evidence separately.
Slack may expose only validated discovery aggregates and reviewed catalog fields through the
read-only `source_inventory_status` surface. It must not expose raw queries, snippets, hashes, notes,
credential metadata, or payloads, and it must not start paid discovery. Paid Slack execution requires
a separately reviewed admin approval design before any execution operation is added.
Every possibly paid call must have a durable `in_flight` marker before HTTP begins. A restart never
retries that indeterminate call silently; an operator must explicitly choose
`--retry-indeterminate`, which records the interrupted attempt before retrying within the fixed
budget. Root-wide execution locking and the persisted completion window enforce one Firecrawl rate
limit across batch IDs.
County research must update the matching Census GEOID link in
`data/source_catalog/county_source_links.csv`, regenerate the state shards with
`python -m grant_watch.coverage_universe --refresh`, and leave every untouched entity explicitly
`not_researched`.

School-district and incorporated-place research follows the same evidence rule. Update the matching
namespaced GEOID link CSV, then regenerate with `python -m grant_watch.school_district_universe
--refresh` or `python -m grant_watch.incorporated_place_universe --refresh`. One source may link to
multiple GEOIDs and one GEOID may link to multiple sources; never collapse that relationship into a
single source field. Census incorporated places are a geography queue, not a unique-government
registry, and do not replace a future county-subdivision/MCD queue.

Never infer that a portal is anonymous, free, statewide, current, or security-relevant from its name.
Keep official ownership, access behavior, integration maturity, and live-result evidence as separate
claims. Never store a credential value in the catalog; record only its environment-variable name.

## Safety boundaries

- Scheduled workers that post to Slack, submit outreach, or write an external system must support and
  honor dry-run. The Socket Mode listener has no dry-run mode; verify it with offline tests unless an
  explicit real-channel interaction is intended.
- Salesforce reads and create-only Campaign actions use separate credentials. Campaign writes remain
  disabled until explicitly approved and sandbox-verified. Organization-only Lead creation must
  resolve exactly one active Salesforce owner from the requesting Slack rep's roster email; never
  default ownership to the integration user or another rep.
- A contact remains `not_found` when public evidence is absent. Never construct or guess an email.
- The production droplet is multi-tenant. Never use admin access, another tenant, `sudo`, or root.
