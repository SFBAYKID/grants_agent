---
name: rfp-discovery-source-fragilities
description: Durable failure modes for the Firecrawl+LLM "rfp" security-RFP discovery poller (grant_watch/sources/rfp.py) — verbatim-but-wrong dates, entity-vs-vendor, multi-RFP cross-contamination, item_id namespacing, relevance block-list, coverage honesty
metadata:
  type: project
---

Design review of the proposed `grant_watch/sources/rfp.py` (Firecrawl SEARCH + SCRAPE + Claude extract → verbatim-verify → SILVER rfp_posted leads). The trust-bearing logic must be PURE code that takes (model_output_dict, scraped_page_text); the LLM is untrusted.

**Why:** this is the first source that mints a lead from an LLM reading an arbitrary page, so the anti-fabrication burden is higher than the free HTTP pollers. Constitution rule 1 (verbatim-verifiable) is satisfied only in letter — not spirit — by a wrong-but-present value.

**How to apply — recurring fragilities to re-check on any RFP-discovery work:**

1. **Verbatim gate proves presence, not meaning.** A page carries many dates (pre-bid meeting, Q&A deadline, addendum, award date). A wrong date extracted as the due date still passes a bare verbatim check → SILVER on a closed RFP. Fix: verify the (deadline LABEL + date) pair are ADJACENT on the page (new pure helper; `_text_field_on_page` flattens the whole page and can't do adjacency). Store the labeled line as `evidence_excerpt`.

2. **Two date representations.** `scoring.grade` needs `item.end` to be ISO (`_parse_date` does `date.fromisoformat(iso[:10])`). But the page prints "Fri, 01/30/2026 - 2:00 PM". Rule: model copies the date VERBATIM → verify that raw substring on page → THEN parse to ISO for `end`. Never verify the ISO form against the page (it won't appear). `finder.verify_on_page` is EMAIL-ONLY (`_EMAIL_RE.fullmatch`) — not reusable for dates/entities; only `_text_field_on_page` is, with the ISO caveat.

3. **Entity = awarder, not a vendor/architect/incumbent named on the page.** No known-entity to bind against (unlike finder). Bind entity to the scraped page HOST (.gov/.k12/.us) + a government-name pattern ("City of", "ISD", "School District", "County", "Township").

4. **Multi-RFP / aggregator index pages are a cross-contamination vector.** One scrape listing 20 RFPs → model returns ONE (non-deterministic drop of 19) or Frankensteins fields across rows (entity row 1 + date row 5, each individually verbatim-present → semantically fabricated lead). v1 recommendation: detect and SKIP list pages (multiple bid-numbers / multiple deadline labels); only accept single-solicitation pages. Aggregators (HigherGov/DemandStar/BidNet) are 403/login-walled anyway.

5. **item_id must be namespaced by entity — the SVPP/CFDA lesson again.** "RFP 2026-05" is NOT globally unique; two cities collide under source="rfp" and one silently overwrites the other. Key: `normalized(entity)|rfp_number`. URL fallback must be normalized (strip query/fragment/trailing slash, lowercase host) or two runs mint duplicates. No cross-source RFP reconcile exists (`reconcile_seed_duplicates` only handles seed↔usaspending).

6. **Relevance: "security" is broad.** Guard services, cybersecurity/information security, SRO/security officer, security deposit, food security are false positives (same trap as `webs._KEYWORD_RE`). Need a deterministic post-filter: allow-list (camera, CCTV, video surveillance, access control, door hardening, badge/card reader, intrusion/alarm, security vestibule, entry control) + block-list. Don't rely on the LLM `category` field alone.

7. **Coverage honesty.** 6 queries × 5 results is a non-deterministic sampling probe, not national coverage; Firecrawl ranking is opaque and changes run-to-run. Never let status/reporting imply completeness. Per-query failures must be isolated (one query's exception must not abort the other five) and must mark the run incomplete — a Firecrawl outage must not read as "no open RFPs this week."

8. **VERIFIED label integrity + drip boundary.** Only set `verification_status=VERIFIED` when entity AND labeled due-date both pass the adjacency gate on the SCRAPED page (never a search snippet). Verified in code 2026-07-18: current `db.nugget_candidates` (gold+award events) and `db.bulletin_candidates` (source IN grants.gov/ca-grants-portal, application_window_opened) will NOT surface a source="rfp" SILVER item — so "drip is a later phase" holds today. Pin it with a regression test; it breaks the moment someone widens bulletin_candidates.

9. **Wiring:** conditional pollers go in `cli._active_pollers()` guarded by an env flag (like SAM_API_KEY), NOT the static `sources.POLLERS` (which runs every poll and would spend money each run). Hard-cap total Firecrawl calls per run.
