# Workflow design — multi-rep leads, Persequor outreach, Salesforce (v1 draft, rev 2)

Status: **DESIGN — nothing here is built until Chase signs off.** Sources: Chase's
direction (2026-07-13 conversation), Persequor's integration response (verified against
its code/prod DB by its agent, same day), and what already ships in `grant_watch/`.
Open decisions are marked ⚠️ OPEN.

Rev 2 (same day): architectural-critic review — verdict *Approved with Required
Changes* — folded in. Its C1 (the old mention-based Persequor handoff was a verified
no-op writing false `contacted`/`sent_at`) was fixed in code immediately: handler
neutered to an honest manual-copy draft, DB audited (zero false rows existed).

---

## 1. Actors

| Actor | Role | Identity |
|---|---|---|
| **Grant** (this project) | finds/scores/surfaces leads; tracks triage state; briefs Persequor | Slack bot `U0BH0ESRJ4W` |
| **Persequor** (`~/monarch_followup_agent`) | drafts + sends email AS a rep, human-approved in the rep's DM | Slack bot; tenant `persequor` on the droplet |
| **Sales reps** (4, verified from Persequor's prod DB) | review statewide leads, claim, approve outreach | chase `U01DPJVURHU` · brett `U08C1NBH875` · kerry `U01E908206M` · anthony `U01DFJWQQJ3` — canonical key is `rep_email` |
| **Salesforce** | CRM of record: accounts, activity, opportunities | sandbox `monarchdev` for dev; prod later |

## 2. Lead lifecycle (end to end)

```
poll (weekly cron) ─► score ─► QUALITY GATE (lead_score rank, top-N only; SHIPPED)
   ─► [SF cross-check: account? owner? last activity? → link + context line]   (§6)
   ─► digest to the leads channel, tagged by state
   ─► rep [🙋 Claim]  → leads.assigned_to = rep  (first click wins, block updates)
   ─► rep [✉️ Draft email]
         │  contact_email verified?  ──no──► lead marked contact:not_found;
         │                                   enrichment queue (Phase 2) — NO brief sent
         yes
         ▼
      Grant POSTs outreach-request.v1 to Persequor  (§4)
         ─► Persequor drafts as the rep → card in the REP'S OWN DM
            (Send / Edit / Dismiss — Persequor's existing approval loop)
         ─► Grant polls status: sent | dismissed | expired | needs_contact | rejected
         ─► Grant reflects outcome into leads/outreach (truthfully, incl. dismissed)
   ─► replies land in the rep's mailbox → Persequor's follow-up machinery (its core)
   ─► real deal → Opportunity in Salesforce (rep-owned; Grant links, never manages)
```

Latency reality (Persequor constraint #1): approval is human-scale — a brief can sit at
`sent_to_rep` for days or die `dismissed`/`expired`. Lead status model must tolerate
that: new → surfaced → claimed → outreach_pending → contacted | back to claimed.

## 3. Multi-rep model

- **Claim**: new button on every digest lead. First tap sets `leads.assigned_to`
  (slack id) + `assigned_at`; the block re-renders showing the owner; later taps get an
  ephemeral "already claimed by @X". Race-safe via a conditional UPDATE
  (`WHERE assigned_to IS NULL`).
- **Only the claiming rep can trigger [Draft email]** for a claimed lead (mirrors
  Persequor's requested_by↔send_as validation; a mismatch is `rejected` on their side —
  we enforce it first on ours for better UX).
- **Unclaimed GOLD re-surfaces** next digest (still ranked); claimed-but-idle leads
  nudge the owner in-thread after 7 days.
- **Rep map** lives in `config/reps.json` (slack_id ↔ rep_email ↔ states[]), the
  slack↔email pairs mirroring Persequor's verified roster. Bad `send_as` is impossible
  by construction: Grant derives it from the claiming rep's entry.
- ⚠️ OPEN (Chase): territory auto-mention (Joe gets @'d for WA leads) — layer on later
  or now? Claim-first works without it.

## 4. Grant ↔ Persequor contract (agreed with its agent, pending Chase's approval)

- **Transport:** `POST /api/v1/outreach-request` on Persequor's FastAPI (to be built on
  its side after Chase approves its architecture doc), plus
  `GET /api/v1/outreach-request/{request_id}` for status. Shared-secret header
  (`X-Persequor-Key`), value provisioned by Chase into both projects' `.env`, never
  committed. Endpoint is draft-only by construction: no parameter can cause a send.
- **Payload:** `outreach-request.v1` JSON — request_id (idempotent; replay returns
  status), entity/entity_type/state/program, `amount_usd` (int, ROUNDED not truncated),
  window dates, source_url, `requested_by_slack` + `send_as` (must map to same rep),
  contact_name/email/title (null = unverified, never guessed), angle, rep_notes, and
  `expires_at` (critic M4: = funds_end, so a card that lingers past the spend window
  can never send a stale-facts email — Persequor's expiry watcher honors it).
- **request_id lifecycle (critic C2 — REQUIRED):** a UUID minted exactly ONCE per
  outreach attempt, persisted on the `outreach` row *before* the first POST, and reused
  verbatim for every retry — network retry, backoff, and post-restart re-queue. A new
  id is minted only for a deliberate resubmission (post-enrichment, or rep-initiated
  after `dismissed`/`expired`). Replay with the SAME id + same payload → status; same
  id + different payload → 409 (critic M3).
- **Grant-side rule:** we do NOT send briefs with `contact_email: null` (Persequor
  would just bounce `needs_contact`) — the button tells the rep "no verified contact
  yet" and queues the lead for enrichment instead. Resubmission after enrichment uses a
  **new request_id** (simpler than PATCH semantics; our preference, flagged to them).
- **Status lifecycle:** `received → drafted → sent_to_rep →` terminal
  `sent | dismissed | expired | needs_contact | rejected`. `sent` is backed by a saved
  Gmail API response on their side. Grant polls (30-min APScheduler tick is plenty at
  human latency) and writes outcomes into `outreach` (`approved_by` = rep_email; their
  approval and send are the same tap) and `leads.status`.
- **Status handling beyond terminals (critic H1):** define reflections for EVERY
  terminal (`sent`→contacted+gmail ids; `dismissed`/`expired`/`rejected`→back to
  claimed with truthful note; `needs_contact`→enrichment queue). A `failed` terminal
  (or Grant-side max-age alarm: brief stuck non-terminal > 5 days → surface to rep +
  Chase, revert to claimed with note) and defined 404-on-known-id behavior are
  round-2 questions to Persequor (§9).
- **Already done (was C1):** the old mention-based handoff is removed from code; the
  interim [Draft email] posts a copyable draft that says plainly the automation isn't
  wired, and changes no state. Persequor-side drafting replaces the template when the
  contract ships — behind a feature flag for one full digest cycle before the template
  is deleted (critic C3).
- ⚠️ OPEN (Chase): wiring — grants_agent on the droplet → localhost-only route;
  staying on the Mac → HTTPS on Persequor's existing host. And provision the shared
  secret. (Also: approve Persequor's architecture doc — its agent won't build the
  endpoint without your sign-off. Both are prerequisites for step 3 below.)

## 5. Noise control (mostly SHIPPED 2026-07-13)

- Quality gate: `scoring.lead_score` (freshness-dominant × dollars × program
  camera-fit), GOLD bucket = top-8 ranked; WATCH never surfaces. Seed/live duplicate
  reconciliation runs after every poll.
- Persequor drips cards one-at-a-time per rep (its Rule 24), so even a burst of briefs
  won't dump on anyone.
- No per-rep send quotas exist on either side yet — if statewide volume gets real,
  Persequor's agent proposes intake-enforced daily caps (Chase would set the number).
- ⚠️ OPEN (Chase): digest destination. #monarch-bot-playground is a noisy bot commons;
  recommend a dedicated **#grant-leads** channel reps actually watch.

## 6. Salesforce integration (design per architectural.md §5.1; build after Persequor v1)

- Read-only v1: match entity → Account/Lead (name+state fuzzy; "possible match" when
  uncertain, never asserted), pull owner + last-activity, render one context line per
  digest lead: `SF: Account owned by anthony, last activity 3d ago → link` /
  `SF: no record — net-new`.
- Auth: JWT Bearer, dedicated least-privilege integration user, sandbox `monarchdev`
  first; prod creds separate. Blocked on: Chase creating the Connected App + user.
- Ownership long-term: SF owner becomes the routing signal (auto-mention that rep);
  Grant's `assigned_to` stays as the interim + cache. Write-back (creating SF Leads) is
  deliberately out of scope until read-only proves accurate.

## 7. Build order (critic C3: enrichment BEFORE the Persequor client — each step ships
tested + verified live before the next)

1. **Claim + rep map + button hardening** — conditional-UPDATE claim; post-claim, ALL
   mutating buttons gate to the owner (critic H4) with `contacted_via` provenance so a
   manual "Mark contacted" is never confusable with a verified send; unclaim/reassign
   commands (critic H3); WAL + busy_timeout on SQLite (critic M8); re-surfacing query
   fix (surfaced-but-unclaimed GOLD reappears — critic M6). Schema delta documented in
   the migration itself.
2. **Phase 2 enrichment** — Firecrawl + Claude extraction to fill contact
   email/name/title; `not_found` stays honest; on completion, notify the claiming rep
   in-thread that [Draft email] went live (critic H5); enrichment-blocked leads are
   excluded from idle nudges. No external dependencies; unblocks everything after it.
3. **Persequor client** — brief builder, POST/poll (30-min tick), status reflection
   matrix, `queued_local` retry with persisted request_id + age cap (critic M5:
   > 7 days → cancel + honest note). Old template path runs behind a feature flag for
   one digest cycle, then deleted. (Gated on: Persequor's endpoint + shared secret +
   Chase's approval of their architecture doc.)
4. **Salesforce read-only context** — needs sandbox Connected App creds.
5. Cron to the droplet (Phase 4 tenant), digest channel move, territory auto-mention.
   Until step 5, the interactive workflow runs on Chase's Mac being awake — accepted
   risk, stated explicitly (critic, Low-f).

## 8. Failure modes designed for

- Persequor down / endpoint 5xx → brief stays queued locally (`outreach.status
  ='queued_local'`), retried with backoff; rep sees "brief queued, Persequor
  unreachable" honestly, never a silent drop.
- Two reps race Claim → conditional UPDATE; loser gets ephemeral notice.
- Rep dismisses the card → Grant records `dismissed` and reverts lead to `claimed`
  (rep can retry with notes) — not `contacted`.
- Enrichment finds nothing → `contact_status='not_found'`, surfaced as such; a human
  can add a contact manually via `/grant contact <lead_id> <email>` (validated, logged).
- Duplicate briefs (double-click) → the persisted-UUID rule (§4) makes any retry carry
  the same id; Persequor's unique index returns existing status, never a second card.
- Rep departs / OAuth disconnects → any roster-reason `rejected` from Persequor raises
  a reps.json-drift alarm to Chase (not silently recorded); their claimed leads are
  reassignable via `/grant reassign` (Chase or owner).
- `/grant contact` manual entry → roster-only, provenance recorded
  (`source_url='manual:<slack_id>'`, confidence medium), domain-vs-entity mismatch
  warning (critic M7) — a typo'd valid address must not become a cold email.

## 9. Round-2 questions for Persequor's agent (before the contract freezes)

1. **Edit-in-Gmail hole (critic H2):** if a rep uses `Edit Email` and sends from the
   Gmail UI, is `sent` ever emitted / `outbound_email` written? If not, what's the
   honest reconciliation so Grant doesn't re-brief an already-emailed contact?
2. `GET` on an unknown/lost request_id → what response, exactly? (redeploy/DB-loss case)
3. Add a `failed` terminal for internal drafting errors, or shall Grant own a max-age
   alarm? Preferred threshold?
4. Will your expiry watcher honor a brief-supplied `expires_at`?
5. Same-id-different-payload: agree on 409?
6. Can you expose a read-only roster GET so `config/reps.json` drift is detectable?

## 10. Test plan (per critic §5 — condensed)

Claim-race (two writers, one rowcount==1); a local FastAPI stub of the contract with
fixtures (replay-same-id, replay-different-payload→409, 404, 5xx→queued_local,
timeout-then-retry-same-id ⇒ exactly one brief); status-reflection matrix (one test per
terminal, `dismissed` never yields contacted); restart recovery (queued_local survives,
same id); null-contact gate (zero HTTP calls, zero rows); `/grant contact` (roster,
provenance, domain warning); migration test (old rows annotated); re-surfacing test.
Live smoke: first real brief = Chase as claimer AND send_as, against Persequor's dev
instance (its dev send-guard restricts to owned mailboxes), full round trip verified —
env-flag gated, never in the default suite.
