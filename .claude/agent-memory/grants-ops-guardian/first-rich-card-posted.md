---
name: first-rich-card-posted
description: The first rich award card ever posted to Slack (2026-08-06) — all 6 blocks accepted, and exactly what Slack rewrites server-side (mailto autolink, verbatim, emoji, block_id)
metadata:
  type: project
---

**First rich card in production: 2026-08-06T20:54:20Z, channel `C01DGT9D11D`, ts
`1786049660.891549`, lead 1603 (Hoxie), snapshot `a0e2069303034c3f863831a844b6a7e3`,
`card_mode=research_needed`, tier gold.** Posted via the one-off (since deleted) on revision
03ab7bb. Verified by reading the message back with `conversations_history`.

**Slack's Block Kit validator accepted all 6 blocks** — `header, section, section, context,
context, actions`. Nothing dropped, nothing rejected, no fallback rendering.

**What Slack REWRITES server-side (expect these when diffing sent vs stored):**
- adds a random `block_id` to every block;
- adds `"verbatim": false` to every `mrkdwn` text object;
- adds `"emoji": true` to the button's `plain_text`;
- **auto-linkifies a bare email into `<mailto:addr|addr>`** — in BOTH the Contact field and the
  top-level `text` field. So the stored `text` is NOT byte-identical to `card.fallback_text`, and
  any future test asserting "every block is a substring of `text`" must account for the mailto
  rewrite on the email specifically.

**Confirmed on the live message:** the `rich_not_relevant` button is present with
`value` == the snapshot id (so the frozen-snapshot binding survives the round trip), and
`re.findall(r"<@U[A-Z0-9]+>")` over the whole message returns **nothing** — no rep was tagged,
which is correct for AR (unmapped) under the 2026-08-05 no-routing-line revision.

**Row shapes written:** `posts` gains `kind='rich_award'`, `style='gold'`, `snapshot_id` set;
`notification_outbox` gains `delivery_class='rich_award'`, `state='delivered'`, and a stable
delivery key of the form `C01DGT9D11D:award:<sha256 of award identity>` — note this is a DIFFERENT
key shape from the legacy nugget's `C01DGT9D11D:lead:<id>:event:<id>`, so the two paths cannot
collide. `integrity_check` ok afterwards.

**EDITED IN PLACE 2026-08-06 21:3x on revision 90f0420** after Chase reacted with "What in the
world is this?" — the original layout was a shouting all-caps name and a cramped side-by-side
two-field block. Same ts, `edited: True`, no new message (channel still showed exactly two bot
messages that day). New shape: `header, section, divider, section, context, context` — **6 blocks
and NO actions block at all**. The "Not relevant" button was REMOVED entirely (Chase: the card is
information, not a control surface), which also made the missing `SLACK_WORKSPACE_ID`
([[slack-workspace-id-missing]]) moot for this card. Name humanized via
`presentation.display_entity_name`, state as a full name ("Arkansas"), spend window and contact
STACKED on separate lines in one section, short dates, third link relabelled "Award record".
Note an actions block with empty `elements` is INVALID Block Kit — omit the block, never emit it
empty. Security composition to preserve: `safe_text(display_entity_name(name, 180), 180)` —
humanize FIRST then escape, because the humanizer strips `<>*_~|@\`` but does not escape `&`.
Probed on the deployed tree: `<!channel>`→`channel`, `@here`→`here`,
`<https://evil.test|click>`→`` (empty), `evil.test`→`eviltest`, `&`→`&amp;`, `<@U01…>`→`U01Dfjwqqj3`.

**Not done in the same session:** the `nces-bind` weekly cron line was authorized but the
`crontab` install was **blocked by the Claude Code permission classifier**; per
[[coordinator-stop-is-stop]] the command was not reshaped and the crontab stayed at 5 lines,
sha `70e309aa…876f`. Related: [[rich-card-enable-20260805]], [[nces-binding-blocks-rich-card]].
