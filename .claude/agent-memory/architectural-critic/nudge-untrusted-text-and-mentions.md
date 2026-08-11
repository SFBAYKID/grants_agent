---
name: nudge-untrusted-text-and-mentions
description: REPRODUCED 2026-08-10 — `_plainify_mentions` only neutralises `<@U…>`, so `<!here>`, `<!channel>`, `<!subteam^…>` and legacy `<@U…|name>` survive a rehearsal; and capability_asks.ask_text (untrusted, scraped from real Slack messages) is embedded verbatim in the message body
metadata:
  type: project
---

# The rehearsal switch does not make a rehearsal safe

`nudges._MENTION_RE = r"<@([UW][A-Z0-9]+)>"`. Executed against every notifying Slack
markup form:

| form | survives plainify? |
|---|---|
| `<@U06RXJKRXSR>` | no — "at Jocelyn" |
| `<@U0STRANGER1>` (off-roster) | no — "a teammate" |
| `<@U06RXJKRXSR\|jocelyn>` (legacy piped) | **YES** |
| `<!here>` / `<!channel>` / `<!everyone>` | **YES** |
| `<!subteam^S012345\|@grants-team>` | **YES** |

That only matters because untrusted text reaches the body. `_capability_message`
embeds `capability_asks.ask_text` verbatim between smart quotes, and that column is
written by `thread_scanner` from real Slack messages, which store mentions in wire
format. Executed end to end: an ask reading `<!here> can grant email <@U01DPJVURHU> the
list?` produced a live send containing `<!here>` (channel-wide ping) and a re-mention
of a third party weeks later — and the REHEARSAL still contained `<!here>`.

Two further consequences of quoting that column verbatim:
- the third party mentioned inside a quote is the ONE named person with no opt-out
  protection: `suppress_reason` checks `target_slack`, `observed['silent_slack']` and
  `observed['tagged_slack']`, none of which is a mention buried in `ask_text`;
- `plain_mentions` rewrites `<@U01DPJVURHU>` to "at Chase" INSIDE the quotation marks,
  silently editing text presented as a colleague's exact words — the justification for
  the quote is "show the words rather than summarise them".

Also confirmed here: `_fair_order` itself is sound. 3,000 random inputs of 0–25 items
across all eight kinds — zero losses, zero duplications, terminates on empty and
single-kind input.

Related: [[nudge-silence-verified-vs-unknown]]
