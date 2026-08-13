# Grant — the Slack chatbot

Grant is the human-facing front of `grants_agent`. It lives in Slack, talks to people, and coordinates
with other Slack agents (notably **@Persequor**, which sends email). Grant is the product's voice, so it
carries the Constitution (`CLAUDE.md`) on its sleeve: **honest, human-in-the-loop, never fabricates.**

## What Grant does

1. **Individual proactive alerts.** Grant never posts multi-lead digests. With the rich-card feature
   OFF (the code default), the existing award/RFP/bulletin ladder is unchanged. Production has the
   flag ON; the rich path permits at most one fully evidenced school/district award card per weekday,
   with Block Kit, fresh public contact, typed read-only CRM context, accurate links, exact/unassigned
   routing, and safe draft/feedback buttons. It never fills the slot with an RFP or bulletin.
2. **Natural engagement.** A human replies in the alert thread for details, or types `@Grant` followed
   by a question in the configured channel. Replies and reactions feed the reward system; Grant does
   not use slash commands, DMs, or help/status menus.
3. **Draft outreach through Persequor.** A natural-language request sends a typed,
   source-linked brief to Persequor, which creates a Gmail draft for human review.
   Grant records `submitted_at` at intake; `approved_by` and `sent_at` remain empty
   until a later verified approval/send status exists. A later explicit “draft again”
   request creates a new draft while redelivery of the same Slack event stays deduped.
4. **Conversation.** Humans can @mention Grant in the configured channel or reply in a proactive alert
   thread. Grant answers from the database and clearly says when it doesn't know.
5. **On-demand search.** A rep @mentions Grant (or talks in a thread) and asks for grants by any
   criteria. Grant **confirms its understanding first** — restating the full filter set and asking how
   many results and which format (Excel / Google Sheet / just in Slack) — then searches its indexed
   database (state, org type, program, grade, amount, record kind, explicit date meaning). Ordering is
   total (an id tiebreak) so a repeated search returns the same rows. Inline results report the true
   match count; complete Excel/Google exports are all-or-nothing under a declared 5,000-row safety cap.
   A bare "@Grant" with no ask gets a friendly greeting.
   Mention-led threads are persisted so a plain follow-up such as “85 is fine—Excel”
   continues the original search instead of being dropped.
6. **Contact enrichment (second step).** After the list, Grant *offers* to find the best contact for
   each org — never automatically, because each lookup scrapes the org's site. On a yes, it enriches
   a bounded top N (maximum 100) with 1–8 workers and one shared 200-call batch budget, adding
   verified-or-honest contact columns to the summary and export. Every runtime Firecrawl call also
   crosses the sole host-bound account ledger: durable monthly ceiling, exact attempt identity,
   cross-process proactive request spacing, and persisted 429/circuit backoff. Email, nearby name,
   title, phone, address, state, and ZIP use field-specific bounded evidence; address components
   cannot cross a page/address block, and a phone-only organization result is labeled a switchboard.
   A genuine clean miss is `not_found`; any unavailable or legacy-indeterminate source leaves the
   result retryable/operator-gated. One request runs organization lookup once and rendering performs
   no network call.
7. **Internal result email.** A rostered rep may ask Grant to email the current frozen search. Grant
   sends one generated bounded Excel attachment through Resend; no caller-supplied path/address is
   accepted, and the temporary workbook is removed after either success or failure.
8. **Source-discovery status.** A rep can ask Grant for the nationwide or state-specific source
   inventory, Census research coverage, manually reviewed candidates, or recent validated batch
   summaries. Supported examples include `@Grant show source discovery status`, `@Grant show
   school-district research coverage in California`, `@Grant list reviewed sources in New Hampshire`,
   and `@Grant show recent discovery batches`. These read-only answers bypass the language model and
   all network-capable tools. They expose validated aggregates and reviewed catalog fields only—not
   raw queries, snippets, hashes, notes, credential metadata, or Firecrawl payloads. A candidate is
   not a lead or working poller. Paid discovery cannot be started from Slack; Grant says it is
   disabled until a separate admin approval workflow exists.

## Honesty rules Grant follows

- Never invents a contact, email, phone, award amount, or a Salesforce "last contacted" date.
- If a contact is `not_found`, Grant says so and offers to let a human research — it does not guess.
- Salesforce matches that are uncertain are shown as "possible match," never asserted.
- Grant has no claim/dibs workflow. Interest leads directly to an exact Salesforce
  lookup or an outreach-draft request.
- Detail replies identify the exact event record and link; a generic source domain is
  never presented as record-level evidence.
- Grant distinguishes discovery dates, application windows, solicitation deadlines, and award spend
  windows. The database does not yet contain a verified award-announcement date, so "received funding
  during this range" is reported as unsupported instead of being mapped to an import or spend date.
- Organization type falls back to conservative name classification while source-provided entity types
  remain sparse; Grant discloses that limitation in filtered results.
- Every send passes through a human. Outreach identifies Monarch Connected, no impersonation, opt-out.

## How Grant is wired (technical)

- **Runtime:** Slack Bolt (Python) in **Socket Mode** — no public URL. Needs `SLACK_BOT_TOKEN` (xoxb),
  `SLACK_APP_TOKEN` (xapp, `connections:write`), `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID` (all in `.env`).
- **Code home:** `grant_watch/slack/` — `grant.py` (bot), `drip.py` (single proactive alerts),
  `search.py` (typed source-aware search), `source_status.py` (read-only discovery UI), and
  `persequor.py` (handoff). Spreadsheet safety and owned temporary artifacts live in
  `grant_watch/spreadsheets.py`; Google Sheets export is Grant's own capability in
  `grant_watch/google_sheets.py`.
- **Talking to Persequor:** Grant submits a draft-only request to Persequor. Persequor
  creates the Gmail draft; nothing is sent by Grant or by intake acceptance.
- **Rich campaign modules:** `grant_watch/campaign/` owns strict policy, bounded preparation,
  durable possibly-paid-call state, exact routing, immutable snapshots, Block Kit, delivery,
  shadow reporting, and actions. `rich-prepare` previews by default; `rich-shadow` is write-free.
  `GRANT_RICH_CARD_ENABLED` defaults OFF in code and is ON in production. Draft-ready actions require
  exact NCES website provenance plus `SLACK_WORKSPACE_ID`; the listener now refuses an enabled but
  unreachable action configuration at startup. The 2026-08-12 audit found the production workspace
  variable absent and 0 NCES websites, so the corrected control surface remains `needs-testing` until
  a reviewed deploy/cutover and human-authorized smoke test.
- **Paid-provider authority:** copied credentials do not authorize this process. Runtime requires
  `GRANT_PAID_PROVIDER_MODE=authority`, a private host capability, and matching standalone Firecrawl
  and ZoomInfo ledgers. The listener and `python -m grant_watch.paid_provider_runtime_check` fail
  closed on a missing/mismatched binding. Cross-machine exclusivity additionally requires
  vendor-side credential rotation/revocation; see `docs/paid_provider_cutover.md`.
- **Google Sheets export (Grant-owned):** email is Persequor's domain; data export is Grant's. Grant
  creates each export as a Sheet in the "Grant Exports" shared drive using its own service account
  (`GOOGLE_SA_KEY_PATH`, `GRANT_EXPORTS_DRIVE_ID`), writes rows with `valueInputOption=RAW` so no cell
  is ever parsed as a formula, shares it with the requesting rep's roster email, and returns the link.
  Persequor is never in this path. Falls back to a complete Excel workbook if unconfigured or on error.
- **Salesforce ownership:** when an approved Campaign action needs an organization-only Lead, Grant
  resolves the requesting Slack rep through `config/reps.json`, finds exactly one active Salesforce
  User with that email, and shows that owner in the immutable preview. It never defaults new Leads to
  Chase or the integration user; missing/ambiguous ownership blocks preparation.
  This owner assignment was verified with one live synthetic Lead in the `monarchdev` sandbox on
  2026-07-16. Complete state/tier requests now freeze one durable server-selected batch and one
  isolated approval per Campaign. Every approved organization must map one-to-one to that child
  action; unresolved identity produces no confirmation button. Campaign members count as added only
  after the returned CampaignMember ID and exact Salesforce readback agree. Timeout replay retains a
  requester-bound read-only reconciliation button and never resubmits the write.
  The IL/FL/TX 67-row Gold+Silver regression is verified offline. Live Campaign membership was
  verified in the `monarchdev` sandbox on 2026-07-25: a multi-turn human-authored Slack thread
  created a Campaign, added two exact existing Leads with verified CampaignMember readback, repeated
  the add without duplicates, blocked one unresolved organization, and added only the explicitly
  approved resolved subset. Typed approval text caused no write; requester-bound button taps did.
  Production writes remain separately credentialed and gated; a human-confirmed action added and
  read back 13 California Gold Leads on 2026-08-10.

## Slack app config

- **App:** "Grant", App ID `A0BH657R5M2`, Monarch workspace (`T01DFJLFKE3`). Installed; bot user `grant`.
- **Icon:** the owl logo (`assets/grant_logo_512.png`). Background color `#0b3d5c`.
- **Socket Mode:** ON (app-level token `grant-socket-mode`, scope `connections:write`).
- **Desired configuration (`needs-testing` externally):** interactivity remains ON for explicit
  Salesforce safety confirmations; events are `app_mention`, `message.channels`, and
  `reaction_added`; no slash command or DM subscription remains.
- **Required bot scopes after reinstall:** `app_mentions:read`, `chat:write`, `channels:history`,
  `channels:read`, `groups:history`, `groups:read`, `reactions:read`, `reactions:write`, `users:read`,
  `users:read.email`, and `files:write`. DM and command scopes are unnecessary.
- **Verified live** (2026-07-13): `auth.test` ok (team Monarch, user grant); `apps.connections.open`
  returned a `wss://` URL. If scopes change later, edit via the app's App Manifest page and reinstall.

## Status

- Rich award-card campaign: `verified` offline with fixtures/fakes for policy, migrations,
  preparation, activity evidence, routing, snapshots, Block Kit, pacing, delivery ambiguity,
  action replay, Persequor wire keys, and frozen thread context. The path is live in production, but
  the exact NCES website writer, workspace startup gate, and reachable-button remediation are local
  only; production activation of those fixes remains `needs-testing` through the guardian.

- Slack app and core bot: `verified` live (provisioned, installed, Socket Mode connected).
- Removal of the live `/grant` command, DM subscription/scopes, and `chat:write.public` is
  `needs-testing`; code already has no handler and fails closed outside the configured channel.
- On-demand search and complete Excel fallback: `verified` offline with pytest on 2026-07-14;
  production configured-channel @mention upload is `needs-testing` through grants-ops-guardian.
- Google Sheets export (Grant-owned service account + "Grant Exports" shared drive
  `0AB7O7rkKxU_jUk9PVA`): `verified` live on 2026-07-14 — end-to-end create → RAW write (formula cells
  stored inert) → numeric amounts → share with rep → URL returned; offline pytest covers the guards,
  value coercion, and the create/share path. Falls back to a complete Excel workbook when unconfigured.
- Confirm-first search UX + two-step contact enrichment: `verified` offline with pytest on 2026-07-14
  (determinism, outage ≠ not_found honesty, cap disclosure, per-org failure isolation, column parity,
  greeting, event-dedup, tool dispatch). Live conversational behavior is `needs-testing` in Slack. The
  design was stress-tested by architectural-critic; enrichment runs inline with a wall-clock budget and
  an idempotency guard — moving it to a background worker is the recommended scaling follow-up.
- Read-only source-discovery status: `verified` offline on 2026-07-15 for natural-language routing,
  exact aggregate boundaries, state/layer filters, adversarial Slack-markup escaping, secret-field
  exclusion, and truthful success/zero/failure/in-flight batch states. Live configured-channel
  interaction remains `needs-testing`; paid discovery is intentionally unavailable in Slack.
- A temporary 20-scenario Slack-delivery harness was run on 2026-07-16 and then deleted. It exposed
  state-code, read-only discovery wording, confirm-first, and unconfigured-Salesforce error paths;
  permanent offline regressions now cover those routes. Delivery/readback was `verified`, while varied
  human `app_mention` ingestion remains `needs-testing` beyond the owner's successful status mention.
- Campaign-batch smoke rendering/delivery was `verified` in `#monarch-bot-playground` on 2026-07-24
  with a labeled, non-interactive result and readback. It performed zero Salesforce writes; live
  Campaign membership was subsequently `verified` end to end in the `monarchdev` sandbox on
  2026-07-25. The two created QA Campaigns each contain exactly the two approved Lead IDs with
  `Identified by Grant` status; the idempotent retry created no duplicate, and the unresolved path
  remained blocked until the requester explicitly chose a resolved-only subset. Production
  Salesforce membership remains `needs-testing`.
- The separate permanent core live verifier is `verified` for the exact Birmingham USAspending award
  and same-card official IT Systems Manager directory record. It makes no Slack or external write and
  does not claim a verified personal email or LinkedIn identity.
- Real-model human-question acceptance is `verified` on 2026-07-16 for 70 scenarios across source
  discovery, lead search/stats/evidence, dates, contacts, LinkedIn, Salesforce reads/previews, web
  research, outreach, lead management, chitchat, unknown facts, cancellation, material corrections,
  missing context, and adversarial requests. Tools are replaced by safe canned outcomes in this
  suite, so it proves language routing and safety—not live external integrations or a human-authored
  Socket Mode event.
- Human-shaped Slack event-envelope acceptance is `verified` offline through the registered Bolt
  `app_mention` and `message` callbacks, including a natural threaded follow-up and receipt deduplication.
  A remote human-authored Socket Mode event is still `needs-testing` because local Slack UI control is
  not approved.
