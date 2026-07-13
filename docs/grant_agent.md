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

## Honesty rules Grant follows

- Never invents a contact, email, phone, award amount, or a Salesforce "last contacted" date.
- If a contact is `not_found`, Grant says so and offers to let a human research — it does not guess.
- Salesforce matches that are uncertain are shown as "possible match," never asserted.
- Every send passes through a human. Outreach identifies Monarch Connected, no impersonation, opt-out.

## How Grant is wired (technical)

- **Runtime:** Slack Bolt (Python) in **Socket Mode** — no public URL. Needs `SLACK_BOT_TOKEN` (xoxb),
  `SLACK_APP_TOKEN` (xapp, `connections:write`), `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`.
- **Manifest / scopes:** `config/grant_slack_manifest.yaml` (paste it to create the app).
- **Code home (when built):** `grant_watch/slack/` — `grant.py` (bot), `digest.py` (message formatting),
  `persequor.py` (handoff). Everything that posts or drafts honors `--dry-run`.
- **Talking to @Persequor:** Grant posts an approved-send message that mentions @Persequor with the draft
  and recipient; Persequor already handles the actual email. The approval gate lives on Grant's side.

## Status

`needs-testing` — spec only. Grant's bot code is Phase 3 and is not built yet (awaiting Chase's full
program description). The manifest and this spec are the setup artifacts Chase needs to provision the app.
