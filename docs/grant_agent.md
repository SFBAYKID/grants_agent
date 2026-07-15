# Grant — the Slack chatbot

Grant is the human-facing front of `grants_agent`. It lives in Slack, talks to people, and coordinates
with other Slack agents (notably **@Persequor**, which sends email). Grant is the product's voice, so it
carries the Constitution (`CLAUDE.md`) on its sleeve: **honest, human-in-the-loop, never fabricates.**

## What Grant does

1. **Individual proactive alerts.** Grant never posts multi-lead digests. A paced cron surfaces at most
   one ranked lead or lower-priority funding bulletin per notification, with strict daily caps. The
   first message is exactly one short factual sentence: no link, buttons, menu, Salesforce context,
   call to action, or extra formatting.
2. **Natural engagement.** A human replies in the alert thread for details, or types `@Grant` followed
   by a question in the configured channel. Replies and reactions feed the reward system; Grant does
   not use slash commands, DMs, or help/status menus.
3. **Approve-to-email.** A natural-language request → Grant composes a personalized draft referencing the specific
   award (amount, program, freshness) → posts the draft **in-thread for a human to review** → on explicit
   human approval, hands the send to **@Persequor**. Grant never sends email itself, and `sent_at` is only
   ever set *after* approval.
4. **Conversation.** Humans can @mention Grant in the configured channel or reply in a proactive alert
   thread. Grant answers from the database and clearly says when it doesn't know.
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
- **Code home:** `grant_watch/slack/` — `grant.py` (bot), `drip.py` (single proactive alerts),
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
