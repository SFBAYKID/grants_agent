# Rep search monitoring — 2026-09-08

Chase requested monitoring of Brett D'Ambrosio's Pennsylvania/Maine spreadsheet
conversation. Production inspection used the grants-ops-guardian and scoped grants
SSH, read-only SQLite queries, and Slack thread readback. No runtime change, message,
CRM action, or paid enrichment was initiated by this audit.

`verified`: the observation window was 11:40:54–11:45:22 AM Pacific. All six
thread receipts completed with delivery recorded. The listener remained unchanged;
there were zero new tracebacks or tool-error entries, the latest keepalive was
healthy, and the watchdog reported nothing stuck. The SSH connection is closed;
no audit monitor remains running.

## Findings

- `verified`: the 11:38 PT request for schools awarded grants "recently" produced
  two complete frozen searches: Pennsylvania 35 award records and Maine 11. Both
  used `org_type=school`, `record_kind=award`, `date_field=award_received`,
  `result_scope=all`, and empty `date_from`/`date_to`. The query sorted verified
  obligation dates without limiting award age.
- `verified`: Pennsylvania's dates span 2019-09-25 through 2025-10-10; Maine's span
  2019-09-24 through 2024-09-30. None of the 46 records falls within six calendar
  months of the request. The existing six-month proactive-card ceiling does not
  apply to requested searches. Returning all years does not fulfill the recent-award
  constraint; the announcement-versus-funds-received caveat does not address age.
- `verified`: both original receipts completed with delivery recorded, and Slack
  thread readback independently showed two hosted Excel attachments, 11,105 and
  7,108 bytes. Both are named `grant_search.xlsx`, consistent with the hardcoded
  export name in `search.py`. This makes state identification unnecessarily difficult.
  Private temporary directories prevent these equal names from overwriting each
  other on the server.
- `verified`: the records contain 34 distinct exact organization names in PA and
  9 in ME. These are award-record counts, not counts of unique schools; Grant's
  original wording correctly called them "school award records."
- `needs-testing`: organization classification quality. Every selected row has an
  empty stored `entity_type`; 23 PA records and no ME records have NCES bindings.
  The name fallback admits "INDIANA COUNTY WILLIAM A. WAUGH PUBLIC SAFETY ACADEMY."
  Its fit with the rep's intended school scope needs authoritative review; its name
  alone does not prove a classification error or establish that it is a K–12 school.
- `verified`: Brett's subsequent NH, Connecticut, and NY requests also executed
  without date bounds. Newest matching dates were 2025-11-12, 2025-06-18, and
  2025-10-10 respectively. NH returned all 3 matches; Connecticut and NY disclosed
  the top 5 of 8 and 86. The inherited recency constraint was still absent.
- `verified`: Brett subsequently requested the complete New York spreadsheet.
  Its 86-record frozen selection spans 2019-08-26 through 2025-10-10. Slack readback
  confirmed the hosted 15,913-byte attachment in the same thread.

## Evidence and limits

- `verified`: production revision marker was `96e9f45`. Local audit HEAD was
  `4d47dba`; this audit did not deploy a revision.
- `verified`: snapshots and tool breadcrumbs agree on the filters. An export job
  marked `created` proves generation, not attachment delivery; the independent
  Slack readback supplies the delivery evidence above.
- `verified`: `search._date_clause` explicitly supports sort-only award dates.
  `test_award_received_without_range_sorts_newest_first` covers that valid feature.
  The bug is using it to answer a recent-award request without an age bound.
- `verified`: current prompt and offline tests intentionally allow anchored
  searches to execute immediately. Missing confirm-first behavior in this thread
  is not a regression. Stale search/date wording in `docs/grant_agent.md` was
  corrected without changing runtime behavior or historical verification labels.
- `needs-testing`: workbook cell contents were not independently downloaded and
  inspected; the counts/date analysis uses the saved selection and current event
  rows. This audit does not prove exhaustive statewide coverage or fresh source
  verification. Existing tests cover workbook generation and upload failures.

## Local health gate

All commands ran from the repository root. Offline verification does not substitute
for production or official-source verification.

| Command | Result |
| --- | --- |
| `python -m pip install -r requirements-dev.txt` | `verified`: exit 0; already satisfied |
| `ruff format --check grant_watch tests` | `verified`: 275 files formatted |
| `ruff check grant_watch tests` | `verified`: pass |
| `vulture grant_watch --min-confidence 60` | `verified`: exit 3, 81 candidates; not a clean gate |
| `python -m grant_watch.health` | `verified`: documentation, annotations, sizes, layout pass |
| `python -m pytest tests -q` | `verified`: 1,768 passed, 90 skipped |
| `python -m grant_watch.source_discovery` | `verified`: 30 checks valid |
| `python -m grant_watch.source_discovery_batch --validate` | `verified`: 1 batch, 27 tasks/attempts, 126 results valid |
| `python -m grant_watch.source_catalog --check` | `verified`: 271 records and generated reports valid |
| `python -m grant_watch.coverage_universe` | `verified`: 3,144 tasks valid |
| `python -m grant_watch.school_district_universe` | `verified`: 13,363 tasks valid |
| `python -m grant_watch.incorporated_place_universe` | `verified`: 32,058 tasks valid |
| `git diff --check` | `verified`: pass |

`verified`: Python source plus tests total 275 files / 89,288 lines. The largest
files are `slack/search.py` (997), `tests/test_nudge_followups.py` (995),
`enrich/salesforce_campaign_gateway.py` (995), `cli.py` (995), and
`enrich/salesforce_campaign_batch.py` (989). They remain below the cap with little
room for additions.

`needs-testing`: full reference-based triage of the 81 Vulture candidates remains
outstanding. Spot checks confirm some flagged helpers are used in tests; decorator
handlers and framework attributes also appear in the output. No candidate was
deleted on Vulture's word.

## Authorized follow-up: recency fix and source verification

The sections above describe the initial audit. The user subsequently authorized live fixes and a
Grant reply in Brett's thread. `verified`: Grant posted and read back one correction at 11:50 PT.
Read-only production queries confirmed zero matching indexed school award records in each of PA,
ME, NH, CT, and NY for 2026-03-08 through 2026-09-08 using existing searchable/verified predicates.
That is a database result, not an exhaustive real-world award claim. Six months is an explicitly
disclosed working default, not a time window specified by Brett.

`verified`: plain recent-award requests now constrain every search attempt before cache lookup to
the inclusive six-calendar-month window. Narrow human state/export/contact follow-ups inherit it;
explicit dates and historical requests supersede it. Bot messages cannot establish that scope.
Incomplete thread pagination discards partial history. Search responses disclose exact dates and
indexed coverage even after zero results or tool-budget exhaustion. XLSX filenames include state.
Mixed date scopes and complex negation are outside the deterministic guarantee.

`verified`: the architectural critic approved the bounded fix and independently passed 48 targeted
tests. The full health gate was rerun: 1,816 tests passed / 90 skipped; formatting passed for 278
files; ruff, health, all six discovery/catalog/universe checks, dependency install and diff checks
passed. Vulture still reports the same 81 pre-existing candidates (exit 3); this is not a clean
dead-code gate. Tests include actual conversation → dispatch → SQL → snapshot → XLSX and failure
paths, inclusive boundaries, retries, contact searches, and incomplete pagination.

`verified`: Pennsylvania has recent official awards missing from the index. Its
[June 3, 2026 approval PDF](https://www.pa.gov/content/dam/copapwp-pagov/en/pccd/documents/schoolsafety/school-safety-award-documents/25-26%20targeted%20school%20safety%20final%20awards%20approved%206-3-26.pdf)
lists 353 applicants: 347 nonpublic schools and six municipal/law-enforcement/approved third-party
applicants. All 353 listed amounts reconcile to $19,356,596. Projects start July 1, 2026, subject to
the document's programmatic/fiscal conditions. Per-row category, funds receipt, and vendor selection
are not established. PDF SHA-256: `e159930d550c2dd828081d47653c3c05a6bbfbded92da1953dfa6702585b4e80`.
The PCCD catalog's reviewed live evidence does not mean a runtime poller exists; none does.

`verified`: a PA/ME workbook was independently reopened and checked after authoring and visual QA.
All 353 PA rows retain applicant, county, IU, amount and PDF page. An independent PDF name-column
extraction matches every applicant after whitespace normalization. The amount formula reconciles to
the official total; approval dates are typed 2026-06-03 values. The Maine sheet contains four
recent school/district examples from official announcements:

- [July 30 greenhouse awards](https://www3.maine.gov/governor/mills/news/governor-mills-announces-500000-grants-help-expand-local-food-production-and-agricultural):
  Limestone Community School, MSAD 33–Valley Unified Education Service Center, and RSU 50. The
  source gives no recipient amounts; cells remain blank rather than splitting the program total.
- [June 12 Augusta Schools award](https://mainedoenews.net/2026/06/12/augusta-schools-receives-later-secondary-school-start-time-planning-grant/):
  $75,000 for later secondary school start time planning, with a June 1–September 10 contract period.

These Maine grants do not establish security-purchase eligibility. This is a partial statewide
research result, not exhaustive coverage or a runtime ingestion. Workbook SHA-256:
`14f9d191adcec9128479aa7d18408b7800b813c494b6487ea91c21510cd25f0c` (25,090 bytes).

`verified`: Connecticut also published an
[August 17 special-education award list](https://portal.ct.gov/governor/news/press-releases/2026/08-2026/governor-lamont-announces-state-grants-to-strengthen-in-district-special-education)
covering 41 programs. It is not included in the PA/ME workbook. Its Brooklyn row has a $1 difference
between programming and total; do not silently normalize source inconsistencies. General education
awards are distinct from proven security procurement eligibility.

`verified`: guardian deployed committed revision `839eef3e312df3a40addf8639e786c49e072eb5f`
from `origin/main` with a consistent integrity-checked DB backup and code backup. All 16 deployed
files match the pinned commit; providers were synced before consumers. Production imports/config
and 53 focused offline tests passed. Only the identified listener was restarted (new PID 879302,
0.166-second measured restart). Environment hash/mtime, crontab, schema 49, and captured database
counts remained unchanged. Post-restart monitoring found zero new tracebacks.

`verified`: at 12:15:37 PT Grant uploaded the PA/ME workbook once in Brett's original thread
(message `1788894937.713059`, file `F0C0C7YC0TG`). Guardian readback independently confirmed Grant's
identity, the exact approved explanatory comment, same-thread placement, filename and 25,090-byte
hosted attachment. The source file hash matched the approved workbook before upload. The reply
explicitly states partial coverage, approval/announcement dates, and unknown security eligibility for
the Maine examples. Source verification for other states remains in progress.

Comparable recent school award lists for NH/NY have not yet been verified by this follow-up.
