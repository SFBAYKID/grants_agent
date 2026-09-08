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

### Verified production execution

The predeployment needs-testing items above are superseded by these executed results.
`verified`: runtime commit `2c9b189a6376c67b7a75eb9542d8acc0e2ac9252` was pushed to main,
deployed through the tenant guardian, and read back at 13:34:07 Pacific. All 159 runtime files
matched the pinned manifest. A consistent SQLite backup passed integrity checking; the previous
changed code was backed up. Thirty-five focused tests passed on the server. The listener restarted
once, from PID 879302 to PID 880240, with clean startup and no current human request interrupted.

`verified`: the production dry run returned the six expected cohorts without changing DB counts.
The authorized real import inserted 401 leads, observations, and funding events. A second real
poll returned zero new rows and appended no events. At 13:36:38 Pacific, actual recent school
searches returned PA293, ME4, NH1, CT41, NY2. The full PA source cohort contains 353 applicants.
All five checked proactive/preparation/nudge pools contained zero reviewed-source records.

`verified`: at 13:37:17 Pacific, every database table count was compared with the consistent backup.
Only leads, source_observations, and funding_events increased by 401 each, plus 12 completed runs.
Posting, CRM, and outreach ledger counts were unchanged. Environment hash/mtime, cron, and schema 49
were unchanged. Six existing human receipts were delivered; the thread still had 18 messages before
final workbook delivery. No new traceback or tool error appeared after deployment/import.

### Workbook verification

`verified`: one workbook was built from the production readback, with Overview and five state tabs.
Every one of 401 names, dates, and monetary values was checked against that readback after export.
All PA county/IU/page values and the $19,356,596 recommended total reconciled. Connecticut's Brooklyn
source conflict retains both reported figures and an empty verified amount. All five data tables
and frozen headers/identifiers survive export. All six worksheets were visually reviewed.

The artifact renderer does not implement HYPERLINK. Its unsupported display was caught in visual
QA and replaced with visible official source URLs; no unsupported formula/cache text remains.
The six summary count formulas calculate correctly, including 401 total records. The exported
workbook is 32,859 bytes, SHA-256
`a9a658abcd0f3311857ebf80e7f682dafbbb325982810a3bcbe67390de273cd4`.

`needs-testing` at this checkpoint: final Slack message/file readback and the five-minute monitor
following that response. No second upload is authorized merely because Slack propagation is delayed.

### Verified thread delivery

`verified`: one upload of the final workbook is hosted in Brett's original thread, file
`F0C0JFGAD28`, message `1788900100.351249` at 13:41:40 Pacific. The guardian read it back at 13:42:11:
Grant authored the exact approved correction, the filename and 32,859-byte size matched, and
thread length increased from 18 to 19. The correction gives all five counts, explains PA conditional
status, CT's amount conflict, NH's expired deadline, and incomplete statewide coverage.

`needs-testing`: remote downloaded-byte equality could not be checked because files.info returned
`missing_scope` for `files:read`. No scope escalation or repeated upload was attempted. Local file
bytes, successful upload receipt, and hosted message/file metadata were verified independently.

Backups retained in the grants tenant:
`/home/grantwatch/grant_watch.db.bak.20260908T203200Z` and
`/home/grantwatch/pre-2c9b189-overwritten.20260908T203200Z.tar.gz`.
The final five-minute monitoring window runs through at least 13:46:40 Pacific.

### Final monitoring and cleanup

`verified`: monitoring ended 13:47:33 Pacific, 5 minutes 53 seconds after the final post. The thread
remained 19 messages with no new human reply. All six original receipts remained complete/delivered;
there were no new tracebacks or tool errors. Thirteen historical tracebacks remained unchanged.
ListenerPID 880240 stayed healthy, all 159 runtime hashes still matched 2c9b189, and the reviewed
cohort remained 401 leads / 401 events with 12 complete, error-free poll runs. Environment, cron, and
schema 49 remained unchanged.

`verified`: remote release/test/upload staging and one-time local deployment/build/export scratch
were removed. Both tenant backups were retained and the SQLite backup integrity was rechecked.
The local output directory retains only the final workbook. Monitoring/SSH were closed after the
bounded verification window; no indefinite monitoring claim is made.

Statewide completeness and automatic discovery of future award cohorts remain outside the evidence
established by this pass. The source refreshes, date filtering, corrected classification handling,
quiet delivery boundary, real index rows, and final thread response were verified as described above.
