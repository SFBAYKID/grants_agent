# Second-pass school-award incident audit — September 8, 2026

User authorized another live pass, fixes, and Grant's responses in Brett's existing thread.
`verified`: Grant posted “Hi Brett, I am gonna do another pass” once at 12:59:12 Pacific,
Slack timestamp `1788897552.531219`, and the guardian read it back in the original thread.

## Predeployment checks

`verified`: the architectural critic reviewed the material design and final implementation.
Its reproduced archive/recipient-binding defect was fixed with current-section extraction.
Its NH date-purpose and exact-fact validation corrections were implemented and tested.
See [source details and limits](reviewed_school_awards.md).

| Health-gate command | Result |
| --- | --- |
| `python -m pip install -r requirements-dev.txt` | verified; requirements satisfied |
| `ruff format --check grant_watch tests` | verified; 287 files formatted |
| `ruff check grant_watch tests` | verified; passed |
| `vulture grant_watch --min-confidence 60` | verified execution; exit 3, 83 findings, not a clean result |
| `python -m grant_watch.health` | verified; documentation, annotations, sizes and tree pass |
| `python -m pytest tests -q` | verified; 1,850 passed, 91 skipped |
| `python -m grant_watch.source_discovery` | verified; 30 immutable checks |
| `python -m grant_watch.source_discovery_batch --validate` | verified; 1 batch, 27 tasks/attempts, 126 results |
| `python -m grant_watch.source_catalog --check` | verified; 276 sources, reports current |
| `python -m grant_watch.coverage_universe` | verified; 3,144 county tasks |
| `python -m grant_watch.school_district_universe` | verified; 13,363 tasks, 67 linked candidates |
| `python -m grant_watch.incorporated_place_universe` | verified; 32,058 tasks |
| `git diff --check` | verified after staging all new fixtures and normalizing trailing whitespace |

Vulture was compared with a temporary checkout of the preceding commit: 81 baseline findings,
83 current findings, three new findings for PCCD TypedDict fields `x0`, `top`, and `bottom`.
Those fields are consumed via dictionary keys in the geometry parser and exercised by the full
353-row test. Eighty baseline findings remain; no unrelated dead-code deletion was attempted.
The first full run also exposed an old test's dependency on the developer's personal database;
that email-link test now constructs a temporary fixture and still checks both email and Slack links.

`verified`: `python -m grant_watch.cli poll --source Reviewed --dry-run` returned PA353,
ME greenhouse3, ME planning1, CT41, NY2, NH1 (401 total), with no database writes.
The six-source live run is separate from fixture verification. Source drift, amount conflicts,
duplicate identity, idempotence, classification, cross-consumer conditional wording, HTTP errors,
redirects, byte limits, and otherwise eligible GOLD proactive exclusions have regression coverage.

`verified`: runtime size at the code phase is 159 Python modules / 49,420 lines. Largest files:
search998, Salesforce gateway995, CLI995, Campaign batch989, Campaign988; the health gate
confirms every text file remains within the 1,000-line limit. No new source module approaches it.

## Production and delivery

`needs-testing` at this predeployment checkpoint: pinned tenant deployment, database ingestion,
repeat-poll readback, final workbook delivery, and monitoring after the final thread response.
The guardian owns all production operations. Their results will be appended after execution.
