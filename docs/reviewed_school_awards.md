# Reviewed recent school-award cohorts — September 8, 2026

These sources repair an observed search-coverage gap in Brett's PA/ME/NH/CT/NY thread.
The first correction's zero results described the indexed database, not the states' actual awards.
The six-month query window is March 8 through September 8, 2026, inclusive. Dates below identify
announcements, conditional approvals, or a dated award letter; none establishes receipt of cash.

## Evidence and limits

`verified`: each parser returned the following live data on September 8. Retained public source
fixtures and fetch hashes are in `tests/fixtures/reviewed_awards/manifest.json`. Fixture tests
separately verify parsing; they are not evidence that a source remains live in the future.

| Source module | Date | Rows | Meaning and limits |
| --- | --- | ---: | --- |
| `sources/pa_pccd.py` | June 3 | 353 | Conditional approvals; 347 nonpublic schools and six other applicants. No row-level applicant categories. Listed recommended total $19,356,596; numeric awarded amounts withheld. |
| `sources/me_greenhouse.py` | July 30 | 3 | K-12 school entities among ten greenhouse recipients. Individual amounts unpublished. |
| `sources/me_start_time.py` | June 12 | 1 | Augusta Schools, $75,000 for later-start-time planning; contract June 1–September 10. |
| `sources/ct_school_awards.py` | August 17 | 41 | Special-education program awards. Brooklyn reports $146,159 programming but $146,160 total; amount withheld and both source values retained. |
| `sources/ny_school_awards.py` | June 12 | 2 | Putnam-Northern Westchester BOCES and KIPP NYC; $5 million each for school-food infrastructure. |
| `sources/nh_school_awards.py` | April 29 | 1 | Rochester School District SAU 54, $60,136.11, reimbursement of 2024–25 transportation costs. June 30 obligation deadline is already past. |

`verified`: all 401 source rows parse. PA's name-based school fallback matches 293 rows, and the
other 60 remain unclassified. Do not call those 60 non-schools or pretend all 347 schools have been
identified individually. A complete PA source export includes all 353 applicants with that caveat.
ME/CT/NY/NH source contexts establish school entities even when names are abbreviated or omit
“school.” Parser-evidenced types survive grading into the search index; names are not expanded.

These are **complete reviewed cohorts, not exhaustive statewide award lists**. Registry entries
refresh these exact documents on the normal poll schedule. They do not discover future award
rounds automatically. New announcements and other programs still require source review and parser
work. General education awards do not prove eligibility to purchase security equipment.

## Access and source binding

All six exact document URLs are pinned in their source modules and in the canonical catalog.
`verified`: anonymous HTTPS fetches returned documents and live parsers reconciled their cohorts.
Redirects are refused, response bytes are capped at 12 MiB, PDF page counts are fixed, and malformed
or changed source evidence raises an error before that poller returns any records.

`verified`: robots files for PA, Maine Governor, Maine DOE Newsroom, CT, and NY allow the reviewed
article/document paths for the poller's user agent. Maine's WordPress robots file recommends its
firehose for regular crawling; this refresh is one pinned document per weekly poll. The NH asset
host's robots file returned an S3 AccessDenied response (403); the public PDF itself returned 200.
No authenticated area or access control was bypassed. Continued access/terms behavior remains
subject to source changes and is not a blanket claim about any portal.

`verified`: Rochester's official `https://www.rochesterschools.com/documents` embeds its public
document API. Full Board Agendas → 25–26 (folder 16773405) explicitly links the exact May 14 PDF.
The selected public document record is retained in `nh_parent_link.json`; the actual NH DOE letter
is on PDF pages 38–39. The district GEOID link is `school_district/3305940`. Administrative GMS
availability, obligation, and reporting dates remain specifically labeled in raw evidence; they
are not mapped onto prospective spending dates.

ME and NY parsers require the current article's recipient section, not names anywhere in the page.
Augusta binds the award, recipient, and amount in its opening article paragraph and verifies
publication metadata. Related/archive mentions cannot replace removed current recipients.
PCCD names, county/IU identifiers, and amounts reconcile against an independent 353-row reviewed
transcription. The two identically named Indian Creek Valley applicants retain separate county/IU
identities; no row-order or amount-based dedup key is used.

## Search-only ingestion boundary

Every record uses `reviewed-school-award:` and defaults to WATCH. Backfill suppression alone does
not block scheduled messaging: existing award queries deliberately admit historical backfills.
The new namespace is therefore excluded independently of grade from drip, daily lists, paid
rich-card preparation, and card nudges before limiting results. Rich delivery rechecks the source
after preparation. Tests use otherwise eligible GOLD rows to prove this boundary.

PCCD's exact conditional source has central record semantics that refuse an awarded-dollar,
received-funds, or established-spend-window claim. Search/export date context, CRM summaries,
outreach payloads, and fallback copy share those semantics. Listed recommended amounts remain
source evidence, not the numeric awarded amount. This is searchable evidence, not proactive
outreach authorization.

## Operational entrypoints

From the repository root:

```bash
python -m grant_watch.cli poll --source Reviewed --dry-run
python -m pytest tests/test_reviewed_award_sources.py tests/test_reviewed_award_boundaries.py -q
```

Only the grants-ops-guardian performs production deployment or ingestion. After the full health
gate and pinned deployment, the same poll without `--dry-run` ingests the reviewed cohorts.
Repeated polling must preserve lead and funding-event counts when source facts are unchanged.
Production execution and thread delivery are recorded separately in the incident audit; this
document's live parser results alone do not establish either.
