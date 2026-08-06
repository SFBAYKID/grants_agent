---
name: authoritative-directory-substring-bind
description: RESOLVED 2026-07-23 (round 2) — policy._authoritative_exact now matches the NCES id as a whole query-value/path-segment; kept for the substring-vs-exact honesty lesson and the draft-ready-gate fix
metadata:
  type: project
---

**RESOLVED 2026-07-23 (round-2 re-review, verified by execution).** `_authoritative_exact`
now parses `parse_qs(query).values()` + `path.split('/')` into a token set and does exact
membership — proven: `062271` no longer binds `?ID=0622710`, `fake0622710` path token no
longer binds `0622710`, `?other=90622710X&ID=9999999` no longer binds `0622710`; exact whole
value and exact path segment still bind. The deeper H2 anchor concern was ALSO fixed, not just
documented: `policy.evaluate` now sets `CardMode.DRAFT_READY` only when
`crm_draft_safe AND provenance in EXACT_WEBSITE_PROVENANCE` (nces/authoritative_directory);
a `verified_org_page` (the `_looks_official` name-anchor) caps the card at `research_needed`
(no auto-draft, no Persequor). Migration 26 adds `leads.nces_website` as the home for the
exact NCES site; NO runtime source populates it, so every real lead is research-only today.
`_same_site` was rebuilt on tldextract registrable-domain (eTLD+1) matching, offline
(`suffix_list_urls=()`), closing the cross-district hole (`montebello.k12.ca.us` vs
`valle.k12.ca.us` → not same-site). Documented residual: a non-PSL private shared-hosting
domain (`txed.net`) collapses subdomains to one registrable — contained because such leads
can never be draft-ready. The lesson below stands.

---

Rich-card gate-loosening branch (`review/rich-award-card-campaign-20260723`, migration 25)
added `policy.ContactBinding.AUTHORITATIVE_DIRECTORY`. The binding test in
`policy._authoritative_exact` (grant_watch/campaign/policy.py:290) is
`return identifier in contact_evidence_url` — a **substring** match, not an exact query-param
/ id-segment extraction.

**Proven false binds** (ran the pure fn): nces_id `"062271"` binds to a URL carrying a
DIFFERENT district `?ID=0622710`; nces_id `"0622710"` binds to `.../fake0622710` and to
`?other=90622710X&ID=9999999` (real district is 9999999). Full `evaluate()` then returns
`eligible=True` with an email at an UNRELATED domain
(`jdoe@some-other-vendor.com`) bound AUTHORITATIVE_DIRECTORY — a contact attributed to the
wrong organization = a rule-1 honesty violation.

**Why:** the docstring + design §16 claim "EXACT, id-bound record ... not a name match", but
the implementation delivers substring binding. The two shipped tests pass by coincidence —
one uses `?ID=0622710` where substring == whole; the negative test uses `ID=9999999` which
happens not to contain the lead id. Neither exercises a substring collision.

**How to apply:** currently INERT and NOT live — flag off, and `finder`/`organization_profile`
never produce an `nces.ed.gov` contact evidence URL (NCES CCD pages carry no staff emails), so
no runtime contact reaches this path. It is a **HIGH latent defect, CRITICAL if the source is
wired**. Before any NCES / authoritative-directory contact source is enabled: replace the
substring test with exact id extraction from the specific `ID=` query param (or an exact path
segment), and add an adversarial test (nces_id `"062271"` + `?ID=0622710` must NOT bind).
This is the same `in`/substring-vs-exact honesty trap seen before in territory tagging
([[soft-state-tags-the-wrong-rep]]) and item_id keying. See also the deeper anchor issue:
`org_website` is derived from the contact's own evidence host (organization_profile._resolve_site),
so "typed, non-heuristic provenance" still rests on `finder._looks_official` (a name heuristic)
end-to-end — the policy layer is only as strong as that enrichment anchor.
