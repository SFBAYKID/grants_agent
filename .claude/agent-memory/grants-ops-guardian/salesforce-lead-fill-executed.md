---
name: salesforce-lead-fill-executed
description: Deploy 17639f8→c36a3e5 and the FIRST bulk fill-leads write to PRODUCTION Salesforce (2026-08-10) — 58 fields written across 9 Leads, CHANGED_FROM_NON_EMPTY 0 and CLEARED 0 proven by read-back; 8 foreign-org ids skipped GET-only; and why exit code 1 on a healthy run is a trap
metadata:
  type: project
---

**Deploy LIVE 2026-08-10T07:30Z.** `17639f8` → `c36a3e59d88220c264bcb9adc7f1669fbe6ec335`.
Listener **41003 → 41662**, **0.139 s outage**. Schema **39**, no migration. 3 files, all
modifications. Closure **123/123**, import closure 120/120, `.env` + crontab untouched,
0 tracebacks. Backup `~/backups/deploy-c36a3e5-20260810T073027Z/`.

## The GET-only property, proven on the deployed bytes

`fill_lead_blanks("00QVC00000Y31aH2AR", {...})` against a **known-foreign** id, with
`requests.patch` and data-API `requests.post` monkeypatched to RAISE:

```
result.success = False
result.error   = 'not in this org'
DATA-API calls = ['GET', 'GET']      <- no PATCH, no POST
```

**Trap in my own first probe:** trapping *all* `requests.post` fired immediately, because
the **OAuth token request is a POST**. That read as "the code tried to write" when it had
not. Filter on `/services/data/` in the URL — the token endpoint is not the data API. Same
family as every other "a failure from a probe you just wrote is a claim about your probe".
Second bug in the same probe: `linked_leads` returns `sqlite3.Row`, so `p[1]` /
`getattr(p,"salesforce_id")` both silently yield nothing — use `r["salesforce_id"]`.

## The write — 58 fields, and the never-overwrite property HELD

`fill-leads --limit 30 --execute`: **considered 21, filled 9, already complete 4,
errored 8**. Verified by reading all 13 records back FROM Salesforce and diffing against a
BEFORE snapshot taken minutes earlier:

```
TOTAL FIELDS WRITTEN (empty -> value) : 58
CHANGED_FROM_NON_EMPTY                : 0
CLEARED                               : 0
```

Biggest fills: Imperial 10, Edison 9, Mammoth 9, Galt 8, Pomona 8. Four leads
(Montebello, Fairfax, Valle Lindo, San Ysidro) had nothing to add. **The read-back is the
only real proof** — the summary line alone cannot distinguish "filled a blank" from
"overwrote something".

The 8 foreign ids each reported `not in this org` and were never patched.

## EXIT CODE 1 ON A COMPLETELY HEALTHY RUN

`cmd_fill_leads` returns `1 if outcome.failed`, and the 8 `not in this org` skips count as
`failed`. So the command **exits 1 after doing exactly the right thing**. If this is ever
put in cron or a script that keys on exit status, a perfect run reads as a failure. The
message was fixed (`HTTP 404` → `not in this org`); the *exit code* still says failure.
Skips are not errors and should not be counted as such.

## MobilePhone: 2 of 4 bought mobiles reached the CRM, exactly as predicted

Landed on **Pomona (lead 239)** and **Imperial (lead 242)**. Did NOT land on **Savanna
(238)** or **Edison (243)**, although a mobile was bought for both — because
`fill_lead_blanks` writes ONE contact per Lead and the title-ranked pick was not the
person carrying the mobile (238: Chief Financial Officer chosen, mobile on Director of
Facilities; 243: Director of Information Systems chosen, mobile on the Superintendent).

**The recommendation given to Chase, and the reasoning:**
- **NEVER** write the mobile from a different contact than the one named on the Lead. A
  Salesforce Lead is ONE person — Name/Title/Email/Phone/MobilePhone all describe the same
  human. Mixing them puts person A's mobile beside person B's title, which is the same
  class of defect already fixed once here (`salesforce_contact_records` falling back to
  the org switchboard for a Lead's `Phone`, so an SDR dialled a switchboard believing it
  was the named person). A wrong mobile is worse than an empty one.
- **Prefer a mobile-carrying contact only as a TIE-BREAK within the same title rank** —
  strictly better, no downside, never demotes a decision-maker.
- Put the *other* bought contact somewhere it keeps its own name and title (a Note on the
  Lead), so the second credit is not wasted and nothing is misattributed.

## San Ysidro stayed empty, and that is correct

`SAN YSIDRO SCHOOLS PUBLIC FINANCING CORP` went 3 → 3 fields. It is a **financing
corporation**, not an operating district — ZoomInfo has no people for it, so there was
nothing honest to write. Anyone reviewing the campaign will see it still looks empty;
that is accurate rather than a miss.

Related: [[contact-fill-first-bulk-buy]], [[salesforce-prod-lead-emptiness]],
[[fill-leads-org-website-laundering]], [[salesforce-writer-fls]].
