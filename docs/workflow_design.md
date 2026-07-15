# Workflow implementation — conversational leads, Persequor outreach, Salesforce (rev 4)

Status (2026-07-14): the local Grant-side workflow is implemented and covered by
offline tests. Live Salesforce reader/Campaign and Persequor endpoint round trips are
**needs-testing**; production cron/deployment is not complete. Open decisions are
marked ⚠️ OPEN.

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
| **Sales reps** (4, verified from Persequor's prod DB) | review leads and request human-reviewed outreach drafts | chase `U01DPJVURHU` · brett `U08C1NBH875` · kerry `U01E908206M` · anthony `U01DFJWQQJ3` — canonical key is `rep_email` |
| **Salesforce** | CRM of record: accounts, activity, opportunities | sandbox `monarchdev` for dev; prod later |

## 2. Lead lifecycle (end to end)

```
poll (weekly cron) ─► score ─► QUALITY GATE (lead_score rank, top-N only; SHIPPED)
   ─► [SF cross-check: account? owner? last activity? → link + context line]   (§6)
   ─► one paced, prioritized lead alert to the configured Grant channel
   ─► rep asks for Salesforce context or a draft (no claim/dibs state)
   ─► rep requests an email draft
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

Latency reality (Persequor constraint #1): approval is human-scale — a draft can sit
for days. Grant records draft submission separately from any later verified send.

## 3. Rep identity model

- Grant does not assign, claim, lock, or reserve leads. Interest triggers a Salesforce
  lookup, contact research, or a Persequor draft request.
- Any configured rep can request a draft. The Slack requester still determines
  `send_as`; Persequor validates that identity before creating the Gmail draft.
- **Rep map** lives in `config/reps.json` (slack_id ↔ rep_email ↔ states[]), the
  slack↔email pairs mirroring Persequor's verified roster. Bad `send_as` is impossible
  by construction: Grant derives it from the requesting rep's entry.
- ⚠️ OPEN (Chase): territory auto-mention (Joe gets @'d for WA leads) — layer on later
  or after the read-only workflow is proven?

## 4. Grant ↔ Persequor contract

- **Transport:** Grant implements `POST /api/v1/outreach-request` to Persequor's
  FastAPI. `GET /api/v1/outreach-request/{request_id}` status reflection is not yet
  implemented. Shared-secret header
  (`X-Persequor-Key`), value provisioned by Chase into both projects' `.env`, never
  committed. Endpoint is draft-only by construction: no parameter can cause a send.
- **Payload:** `outreach-request.v1` JSON — request_id (idempotent; replay returns
  status), entity/entity_type/state/program, `amount_usd` (int, ROUNDED not truncated),
  window dates, source_url, `requested_by_slack` + `send_as` (must map to same rep),
  contact_name/email/title (null = unverified, never guessed), angle, rep_notes, and
  `expires_at` (critic M4: = funds_end, so a card that lingers past the spend window
  can never send a stale-facts email — Persequor's expiry watcher honors it).
- **request_id lifecycle:** a deterministic ID is minted once per triggering Slack
  event, persisted before the first POST, and reused for network retry/redelivery. A
  later explicit “draft again” Slack event gets a new ID and a fresh Gmail draft.
  Replay with the SAME id + same payload → status; same
  id + different payload → 409 (critic M3).
- **Grant-side rule:** we do NOT send briefs with `contact_email: null` (Persequor
  would just bounce `needs_contact`) — Grant tells the rep "no verified contact
  yet" and queues the lead for enrichment instead. Resubmission after enrichment uses a
  **new request_id** (simpler than PATCH semantics; our preference, flagged to them).
- **Expected external status lifecycle:** `received → drafted → sent_to_rep →` terminal
  `sent | dismissed | expired | needs_contact | rejected`. `sent` is backed by a saved
  Gmail API response on their side. Grant polls (30-min APScheduler tick is plenty at
  human latency) and writes outcomes into `outreach` (`approved_by` = rep_email; their
  approval and send are the same tap) and `leads.status`. Grant-side status reflection
  remains **needs-testing/not yet implemented**.
- **Status handling beyond terminals (critic H1):** define reflections for EVERY
  terminal (`sent`→contacted+gmail ids; `dismissed`/`expired`/`rejected`→surfaced
  with truthful note; `needs_contact`→enrichment queue). A `failed` terminal
  (or Grant-side max-age alarm: brief stuck non-terminal > 5 days → surface to rep +
  Chase, retain as surfaced with a note) and defined 404-on-known-id behavior are
  round-2 questions to Persequor (§9).
- **Implemented locally:** Draft Email builds an event-safe typed brief, persists one
  request id before network I/O, submits it, and retains bounded backoff state for the
  retry worker. If the endpoint is unavailable, Grant presents honest fallback copy;
  it does not claim an email was sent.
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
- Grant uses one explicitly configured alert channel. Multi-lead digests are disabled globally;
  changing the channel does not enable them.

## 6. Salesforce integration

- Read-only v1 is implemented locally: match entity → Account/Lead, then only
  account-bound open Opportunities. Status is explicit (`found`, `no_match`,
  `ambiguous`, `partial`, `unavailable`), and a verified open Opportunity receives the
  largest proactive-ranking boost. A bounded sync stores links/owners locally so
  message selection does not make unbounded live calls.
- Auth: OAuth client credentials, dedicated least-privilege Connected App run-as user, sandbox
  `monarchdev`
  first; prod creds separate. Blocked on: Chase creating the Connected App + user.
- Live sandbox authentication and match-quality sampling remain **needs-testing**.
- Campaign intake is the only write exception: separate credentials, feature flag off,
  exact preview, same-user Slack confirmation, create-only allowlist, and durable
  per-record outcomes. New organization Leads may be created only to become Campaign
  Members; Account/Contact/Opportunity records are never modified.

## 7. Deployment order

1. Complete local test/quality gates and commit a reviewable increment.
2. Run Salesforce reader and Campaign shadow tests against the sandbox; keep writes off.
3. Verify one Chase-owned Persequor test-mode brief and implement status reflection.
4. Have grants-ops-guardian validate the scoped Unix user/key/database role and deploy
   only a committed revision into the grants tenant.
5. Schedule poll → Salesforce sync → individual drip → outreach retry. Enable Campaign
   writes only after preview logs are reviewed and a second explicit approval.

## 8. Failure modes designed for

- Persequor down / endpoint 5xx → brief stays queued locally (`outreach.status
  ='queued_local'`), retried with backoff; rep sees "brief queued, Persequor
  unreachable" honestly, never a silent drop.
- Two reps request drafts → each explicit Slack request receives its own draft ID;
  redelivery of either Slack event remains idempotent.
- Rep dismisses a draft → Grant records `dismissed`; the lead remains surfaced and
  can be drafted again later — it is not marked `contacted`.
- Enrichment finds nothing → `contact_status='not_found'`, surfaced as such; a human
  can provide a contact naturally in the thread (validated and logged when supported).
- Duplicate briefs (double-click) → the persisted-UUID rule (§4) makes any retry carry
  the same id; Persequor's unique index returns existing status, never a second card.
- Rep departs / OAuth disconnects → any roster-reason `rejected` from Persequor raises
  a reps.json-drift alarm to Chase rather than silently recording success.
- Manual contact entry → roster-only, provenance recorded
  (`source_url='manual:<slack_id>'`, confidence medium), domain-vs-entity mismatch
  warning (critic M7) — a typo'd valid address must not become a cold email.

## 9. Remaining Persequor contract questions

1. **Edit-in-Gmail hole (critic H2):** if a rep uses `Edit Email` and sends from the
   Gmail UI, is `sent` ever emitted / `outbound_email` written? If not, what's the
   honest reconciliation so Grant doesn't re-brief an already-emailed contact?
2. `GET` on an unknown/lost request_id → what response, exactly? (redeploy/DB-loss case)
3. Add a `failed` terminal for internal drafting errors, or shall Grant own a max-age
   alarm? Preferred threshold?
4. Will your expiry watcher honor a brief-supplied `expires_at`?
5. Same-id-different-payload: agree on 409?
6. Can you expose a read-only roster GET so `config/reps.json` drift is detectable?

## 10. Verification plan

Offline coverage includes idempotent queued retry, null-contact gating, migrations,
search snapshots/exports, Salesforce ambiguity/outages, and Campaign
preview/nonce/channel/partial-timeout cases. Live smoke remains gated: the first brief
is Chase as requester/send-as in test mode; the first Salesforce Campaign action
targets the sandbox and is reviewed from its immutable preview before the writer flag
is enabled.
