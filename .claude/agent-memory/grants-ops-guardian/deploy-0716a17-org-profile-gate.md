---
name: deploy-0716a17-org-profile-gate
description: Deploy 65f05c7→0716a17 on 2026-08-09 (CURRENT PROD) — code-only, schema stayed 37, PID 30207, but a ~47s outage because my restart script pkill'd WITHOUT relaunching; then 27 real Salesforce field writes across 5 Leads with a clean never-overwrite proof
metadata:
  type: project
---

**LIVE 2026-08-10T03:02:12Z (droplet Sun 20:02 PT).** `65f05c72894f7d442d783396a2c6d4c3fe9a402d`
→ `0716a177b3c8c942f0ed468c698d5e8c372671d2` (2 commits). **CODE-ONLY — schema stayed 37**
(`git diff 65f05c7..0716a17 -- grant_watch/migrations.py` empty; MAX 37 before and after).
Delta 11 paths = **4 deployable** + 7 `.claude/agent-memory/**`. All 4 modifications, zero
adds, zero deletes. Pre-image: all 4 hashed byte-exact at 65f05c7 — clean base, no drift.
Import closure **114 modules, 0 failures**. Second dry run empty. `.env`, `run_bot.sh`,
crontab (12 lines, nudge line `grep -cxF` == 1) all byte-identical before and after.

## THE ONE THING THAT WENT WRONG: pkill is not a restart

My `restart.sh` did `pkill -f 'grant_watch[.]slack[.]grant'` and then only **waited** for a
new PID — I had internalised the memory line "let the */5 cron keepalive relaunch it" and
dropped the `nohup bash run_bot.sh` that every previous deploy actually ran. The wait loop
timed out, `PID_COUNT=0`, the fresh log region was empty, and **the bot sat dead**. Outage
**~47 s** (kill 20:01:25 PT → PID 30207 at 20:02:12 PT) versus 0.2–4 s on every prior deploy.

The keepalive *would* have recovered it, but only at the next `*/5` boundary — so trusting
it turns a sub-second outage into an up-to-5-minute one. **The restart is `pkill` THEN
`nohup bash run_bot.sh >> cron.log 2>&1 < /dev/null &`.** The cron keepalive is the safety
net for a crash, never the deploy's relaunch mechanism.

The tell was correct and immediate: an EMPTY fresh log region plus `PID_COUNT=0`. A restart
verifier that only greps for "Bolt app is running" would have reported a bland failure;
printing the PID count and the raw fresh region is what named the cause in one look.

## `--delete` preview prints `*deleting`, NOT `deleting` — my counter read 0 while 100+ scrolled past

`grep -c '^deleting'` returned **0** on a preview containing well over a hundred
`*deleting   …` lines, including `secrets/google_sheets_sa.json` and all of `docs/`. Only
printing the preview text caught it. Exactly the family in [[deploy-mechanism]]'s "never
trust a zero from a pattern you have not seen match" — count with `grep -ci 'deleting'` and
always print the head of the preview.

And the deletions themselves are the expected artifact of `--delete` against a **partial**
staging dir. The justification for omitting `--delete` is the git delta having zero `D`
entries, never the preview.

## THE EVIDENCE DISTINCTION THAT CLEARED TWO LEADS: bare host vs deep link

[[fill-leads-org-website-laundering]] stopped this command because `org_website` held
`https://cde.ca.gov`. Leads #234/#235 also have **contacts** whose `source_url` is on
cde.ca.gov — and those are FINE, because they are
`cde.ca.gov/schooldirectory/details?cdscode=15634610000000`: the state directory's record
**for that specific district**. The email local-parts match the names (`lbrown` ↔ Lora
Brown, `eevans` ↔ Elizabeth Evans) and the domains are the districts' own
(`fairfaxsd.us`, `sd.vallelindo.k12.ca.us`), not the state's.

> Evidence quality lives in the URL's **specificity**, not its domain. A bare host is what a
> failed search fell back to; a deep link with the record's own key is a citation.

So the same domain is junk in `org_website` and authoritative in `contacts.source_url`, and
the gate is right to withhold one while keeping the other. Do not "clean up" cde.ca.gov by
domain.

## The gate is PROVEN at the destination, not just in the preview

`fill-leads --limit 5` (dry) then `--execute`, then a read-back **from Salesforce**:

| lead | entity | `org_profile_status` | Website in Salesforce now |
|---|---|---|---|
| 231 | Birmingham Community Charter HS | `found` | `https://bcchs.net` |
| 232 | Montebello Unified | `found` | `https://montebello.k12.ca.us` |
| 233 | San Ysidro Schools Public Financing Corp | `not_found` | **None** (was going to be the finalsite CDN) |
| 234 | Fairfax Elementary | `not_found` | **None** (was going to be cde.ca.gov) |
| 235 | Valle Lindo | `not_found` | **None** (was going to be cde.ca.gov) |

**27 fields filled, all from EMPTY.** Never-overwrite verdict PASS: `CHANGED_FROM_NON_EMPTY`
0, `CLEARED` 0. Every one of the five Leads had exactly ONE non-empty allowlisted field
beforehand — `State='CA'` — and all five still read `'CA'`, which makes State the single
clean canary for the guarantee. `IsConverted`/`Status` unchanged on all five.

The 11 allowlisted fill fields (`_ALLOWED_LEAD_FILL_FIELDS`): Street, City, State,
PostalCode, Phone, MobilePhone, Email, Title, Website, Industry, Number_of_Students__c.

**Recipe worth reusing:** run the SAME capture script for before and after with an output
path argument, so the diff compares two identically-produced files instead of two ad-hoc
reads. Classify every changed field by what it was BEFORE; the "was non-empty" bucket is
the property under test and must be empty.

## A SECOND CALLER IS AFFECTED, and nobody counted it

`organization_fields` has two callers, not one:
`salesforce_lead_fill.proposed_fields` **and**
`enrich/salesforce_campaign_ownership.py:47` (`payload.update(organization_fields(row))`) —
the **create**-a-new-org-Lead path, which is armed in prod
([[campaign-writes-flag-armed-in-prod]]). So this gate also silently changed what NEW Leads
carry: a `not_found` org no longer gets Street/PostalCode/Website. Strictly an improvement
(it can only omit, never alter), but it was outside the stated blast radius. **Grep the
callers of any function a "one command's" fix touches.**

Still ungated, deliberately flagged not fixed: `choose_phone` (same module) falls back to
`org_phone` with no `org_profile_status` check, and feeds the contact-record payload paths
at lines 376/478/645 — a different surface from `fill-leads`.

## Postflight fingerprints

`.env` `9b68bc18…c634` (67 lines / 33 keys), crontab `63495d44…a7f7` (12 lines),
`run_bot.sh` `07773019…06bb` — all identical to pre-deploy. Schema MAX 37, 46 tables,
`integrity_check` ok, `foreign_key_check` EXACTLY the two approved orphans (10642, 11892),
leads 10715, contacts 85, **`followup_nudges` 0** (nothing messaged anyone). Disk 67%, 16 G.
The pre-deploy DB `VACUUM INTO` copy hashed **identical** to the previous deploy's
(`0bec2cf8…c986`) — consistent with the live DB mtime never having moved since 65f05c7, i.e.
zero DB writes in between. A matching vacuum sha is a cheap "nothing happened here" proof.

## Rollback artifacts (700/600) — `~/backups/deploy-0716a17-20260810T025846Z/`

- `grant_watch.db.vacuum` 25,096,192 B sha `0bec2cf8244c97642a48688fa53e8dcf9c8b851e08e07070e459e0577ac6c986`
  (COPY verified: integrity ok, schema 37, 46 tables, leads 10715, followup_nudges 0)
- `code_at_65f05c7.tar.gz` 38,336 B sha `318278d767c9927f8e9449a38b092f151346b2c77984d934f75dac7d71290040`, 4 members, `gzip -t` OK
- `env.bak`, `crontab.bak`, `deployed_revision.bak`
- `salesforce_before.json` / `salesforce_after.json` — the CRM snapshots, retained here
  because **the Salesforce writes have no rollback**; this file is the only record of what
  those 5 Leads held beforehand.

Rollback of the CODE = restore the tar, re-stamp 65f05c7, restart. No DB rollback wanted.

Related: [[deploy-mechanism]], [[deploy-65f05c7-fill-leads-fix]],
[[fill-leads-org-website-laundering]], [[verify-the-premise-not-the-claim]],
[[ssh-rate-limit-and-stdin-traps]].
