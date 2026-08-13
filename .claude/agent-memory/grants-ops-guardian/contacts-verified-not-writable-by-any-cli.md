---
name: contacts-verified-not-writable-by-any-cli
description: "contacts.contact_status='verified' has exactly ONE writer and it is the interactive Slack tool - no CLI/cron path can ever produce it, and contact_evidence is a DIFFERENT table"
metadata:
  type: project
---

**NO BATCH COMMAND CAN PRODUCE A `verified` CONTACT. THE ONLY WRITER IS A HUMAN IN SLACK.**

**Why:** measured 2026-08-13 while trying to repair [[migration-46-total-quarantine]].
`db.save_contact` is the sole writer of `contacts.contact_status='verified'`
(`db_contacts.py:145`, `VALUES (...,'verified',...,'page_verified')`) and it has exactly
**one caller in the entire codebase**: `grant_watch/slack/contact_enrichment.py:119`,
reached only through the `find_contact` Slack tool. `contact_enrichment` is imported only
by `slack/tools.py`. So a goal phrased as "turn the 68 unverified contacts into verified"
is **unreachable at any budget** by any CLI command.

**How to apply:** before accepting a task to "restore verified contacts", check which
SURFACE the metric lives on. Ask for a projector or a rep-driven Slack pass; do not spend
paid credits expecting the label to move.

## The two surfaces are different tables, and they disagree by design

| write path | table written | resulting label |
|---|---|---|
| `find_contact` Slack tool -> `save_contact` | `contacts` | **`verified`** + `field_evidence_json` |
| `rich-prepare --execute` -> `contact_evidence.refresh` | `contact_evidence` | `verified` row, **no `contacts` row at all** |
| `fill-contacts --execute` (ZoomInfo) | `contacts` | `vendor_licensed` - hardcoded at `db_contacts.py:218`, and `migrations_zoominfo.py:39` says "**Never page-verified**" |
| `save_linkedin_contact` | `contacts` | `linkedin_only` |
| `mark_contact_not_found` | `contacts` | `not_found` |

Proven on production: lead **3927 CALDWELL COUNTY SCHOOLS** gained a `contact_evidence`
row `status='verified'` (Terry Sullivan, Director of Technology, exact evidence URL +
hash) while `contacts` holds **zero rows for that lead**. Same shape as
[[card-contact-may-live-only-in-snapshot]] - a contact can be real, evidenced and
invisible to the surface that gates outreach.

**The outreach gate reads `contacts`, not `contact_evidence`**: `nudge_promises` and
`db.contacts_for_lead` order on `contact_status='verified'`, and
`db_contacts.contact_is_page_verified` returns False for anything else. So
`vendor_licensed` purchases and `contact_evidence` verifications both leave
`_request_outreach` refusing.
