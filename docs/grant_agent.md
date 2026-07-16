# Grant — the Slack chatbot

Grant is the human-facing front of `grants_agent`. It lives in Slack, talks to people, and coordinates
with other Slack agents (notably **@Persequor**, which sends email). Grant is the product's voice, so it
carries the Constitution (`CLAUDE.md`) on its sleeve: **honest, human-in-the-loop, never fabricates.**

## What Grant does

1. **Weekly digest.** On the Monday-morning cron, Grant posts to its channel: new 🥇 GOLD leads, new 🥈
   SILVER leads, and ⏳ expiring-window alerts (spend/obligation deadline < ~90 days). One block per lead:
   entity, state, program, $, window, contact (if found), a one-line "why now," and a Salesforce note
   ("already an Account — last activity 3 days ago · <link>" or "net-new — no CRM record").
2. **Interactive triage.** Each lead shows buttons: **[Draft email] [Mark contacted] [Snooze] [Bad lead]**.
   [Bad lead] reasons feed back into scoring.
3. **Approve-to-email.** [Draft email] → Grant composes a personalized draft referencing the specific
   award (amount, program, freshness) → posts the draft **in-thread for a human to review** → on explicit
   human approval, hands the send to **@Persequor**. Grant never sends email itself, and `sent_at` is only
   ever set *after* approval.
4. **Conversation.** Humans can @mention Grant or DM it to ask about a lead, re-post a digest, or check a
   district's status. Grant answers from the database and clearly says when it doesn't know.
5. **On-demand search.** A rep @mentions Grant (or talks in a thread) and asks for grants by any
   criteria. Grant **confirms its understanding first** — restating the full filter set and asking how
   many results and which format (Excel / Google Sheet / just in Slack) — then searches its indexed
   database (state, org type, program, grade, amount, record kind, explicit date meaning). Ordering is
   total (an id tiebreak) so a repeated search returns the same rows. Inline results report the true
   match count; complete Excel/Google exports are all-or-nothing under a declared 5,000-row safety cap.
   A bare "@Grant" with no ask gets a friendly greeting.
6. **Contact enrichment (second step).** After the list, Grant *offers* to find the best contact for
   each org — never automatically, because each lookup scrapes the org's site (~30s). On a yes, it
   enriches the top N (capped at 10 to stay responsive), adding verified-or-honest contact columns to
   the summary and the export. A contact is stored only if its email appears verbatim on a fetched page;
   a genuine miss is `not_found`; a source outage records **nothing** (retryable) — never a false
   `not_found`.
7. **Source-discovery status.** A rep can ask Grant for the nationwide or state-specific source
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
- Grant distinguishes discovery dates, application windows, solicitation deadlines, and award spend
  windows. The database does not yet contain a verified award-announcement date, so "received funding
  during this range" is reported as unsupported instead of being mapped to an import or spend date.
- Organization type falls back to conservative name classification while source-provided entity types
  remain sparse; Grant discloses that limitation in filtered results.
- Every send passes through a human. Outreach identifies Monarch Connected, no impersonation, opt-out.

## How Grant is wired (technical)

- **Runtime:** Slack Bolt (Python) in **Socket Mode** — no public URL. Needs `SLACK_BOT_TOKEN` (xoxb),
  `SLACK_APP_TOKEN` (xapp, `connections:write`), `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID` (all in `.env`).
- **Code home:** `grant_watch/slack/` — `grant.py` (bot), `digest.py` (message formatting),
  `search.py` (typed source-aware search), and `persequor.py` (handoff). Spreadsheet safety and owned
  temporary artifacts live in `grant_watch/spreadsheets.py`; Google Sheets export is Grant's own
  capability in `grant_watch/google_sheets.py`.
- **Talking to @Persequor:** Grant posts an approved-send message that mentions @Persequor with the draft
  and recipient; Persequor already handles the actual email. The approval gate lives on Grant's side.
- **Google Sheets export (Grant-owned):** email is Persequor's domain; data export is Grant's. Grant
  creates each export as a Sheet in the "Grant Exports" shared drive using its own service account
  (`GOOGLE_SA_KEY_PATH`, `GRANT_EXPORTS_DRIVE_ID`), writes rows with `valueInputOption=RAW` so no cell
  is ever parsed as a formula, shares it with the requesting rep's roster email, and returns the link.
  Persequor is never in this path. Falls back to a complete Excel workbook if unconfigured or on error.

## Live Slack app config (provisioned 2026-07-13 — this is the record; the setup manifest was removed)

- **App:** "Grant", App ID `A0BH657R5M2`, Monarch workspace (`T01DFJLFKE3`). Installed; bot user `grant`.
- **Icon:** the owl logo (`assets/grant_logo_512.png`). Background color `#0b3d5c`.
- **Socket Mode:** ON (app-level token `grant-socket-mode`, scope `connections:write`).
- **Interactivity:** ON. **Events:** `app_mention`, `message.im`. **Slash command:** `/grant`.
- **Bot scopes (17):** `app_mentions:read`, `chat:write`, `chat:write.public`, `commands`,
  `channels:history`, `channels:read`, `groups:history`, `groups:read`, `im:history`, `im:read`,
  `im:write`, `mpim:history`, `reactions:read`, `reactions:write`, `users:read`, `users:read.email`,
  `files:write`.
- **Verified live** (2026-07-13): `auth.test` ok (team Monarch, user grant); `apps.connections.open`
  returned a `wss://` URL. If scopes change later, edit via the app's App Manifest page and reinstall.

## Status

- Slack app and core bot: `verified` live (provisioned, installed, Socket Mode connected).
- On-demand search and complete Excel fallback: `verified` offline with pytest on 2026-07-14;
  production DM/@mention upload after this fix is `needs-testing` through grants-ops-guardian.
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
