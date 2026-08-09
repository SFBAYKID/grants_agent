---
name: human-asserted-row-verified
description: First live human_asserted contact row verified in prod 2026-08-09 — the safety property holds; the "verified must still be 19" invariant moved to 20 for an UNRELATED reason
metadata:
  type: project
---

**The first ever `record_contact_fact` write is verified correct in production** (contacts id **84**,
lead **4897 SCOTTSBLUFF PUBLIC SCHOOL**, NE, gold). `contact_status='human_asserted'`,
`provenance='human_asserted'` (non-NULL), `asserted_by_slack_user='U01DPJVURHU'`,
`asserted_at='2026-08-09T22:28:29+00:00'`, **phone populated, email empty**, title empty. Inserted as
its OWN row — the two `vendor_licensed` rows on that lead (ids 82 David Davis, 83 James Todd) are
untouched, so lead 4897 now has **3** contact rows: 2 vendor_licensed + 1 human_asserted.
David Davis therefore appears twice on the lead, by design: the vendor row carries an email and no
phone, the asserted row a phone and no email.

**THE SAFETY PROPERTY HOLDS.** A full status×provenance cross-tab of all 85 rows is perfectly
diagonal — `human_asserted`↔`human_asserted` (1), `verified`↔`page_verified` (20),
`linkedin_only`↔`linkedin_claimed` (36), `vendor_licensed`↔`vendor_licensed` (2),
`not_found`↔NULL (26). No asserted fact is labelled `verified`, and no row mixes classes.

**BUT THE STATED INVARIANT "verified must still be 19" WAS ALREADY FALSE AT BASELINE — 20 — AND IT
WAS NOT A BUG.** The 20th is contacts id **85**, a genuine `page_verified` row on a DIFFERENT lead
(**4898 MEDICINE VALLEY PUBLIC SCHOOLS**, Scott Trimble, Superintendent, source
`mvraiders.org/en-US/our-superintendent-…`, confidence high, evidence JSON present, phone + email
populated). Same Slack session, different lead. **Lesson: a global count is not an invariant of one
code path — concurrent legitimate activity moves it. Localise the assertion (cross-tab, or count
by lead) before calling a count drift a safety failure.** Cf. the derived-count lesson in
[[deploy-2239a18-human-asserted]].

**Proof of "unchanged" came from the previous deploy's own backup.** `~/backups/
deploy-2239a18-20260809T221928Z/grant_watch.db.pre32` was taken at 22:19:28Z, **before** the 22:28:29Z
assertion, so it is a true pre-state: it holds contacts total **83**, verified **19**, lead 4897 rows
**2**. Diffing the 14 shared columns for ids 82/83 live-vs-backup returned **zero differences**. Keep
doing this — a deploy backup doubles as a forensic baseline for anything that happened after it.

**Reusable gotchas:**
- **`contacts` has NO `created_at`/`updated_at`.** Its 17 columns are id, lead_id, name, title, email,
  phone, source_url, confidence, contact_status, official_domain, field_evidence_json,
  contact_provenance, do_not_call, vendor_person_id, provenance, asserted_by_slack_user, asserted_at.
  Order by `id` to find the newest row; a `created_at` query just raises `OperationalError`.
- Report contact PII as populated-or-not plus `length()`, never the value — that satisfies "is the
  phone there?" without echoing it.
- `sqlite3` `mode=ro` on the hot WAL served every one of these reads with zero writes
  ([[readonly-db-forensics-recipe]]).
- Capture this kind of evidence BEFORE a deploy's `__pycache__` purge and restart, per
  [[pycache-purge-destroys-forensics]]; the writer modules' `.pyc` mtimes were all 1786314180
  (22:23:00Z, the prior deploy's first import) and the purge would have erased them.
