---
name: kerry-email-sent-and-the-15-row-cap
description: Deploy 850cccc→801b762 and the FIRST real lead email to a rep (2026-08-10) — the for_chat fix works, but a separate 15-row display cap means the email carries 15 of 81; it discloses that itself, which is why it was safe to send
metadata:
  type: project
---

**Deploy LIVE 2026-08-10T21:29Z.** `850cccc` → `801b762af4d13cda2ed269564f5a205546f17d0a`
(2 commits: `8831e94` then `801b762`). Listener **55908 → 57077**, **0.147 s outage**.
Schema **39**, no migration. 5 files, all modifications. Closure **123/123**, import
closure 120/120, `.env` + crontab byte-identical, 0 tracebacks. **TOOL_SCHEMAS 22 → 23**
(`zoominfo_fill_many` present). Backup `~/backups/deploy-801b762-20260810T212905Z/`,
rollback tar member count **asserted == 5 before trusting it** (see below).

## TOPOLOGY: read it before deploying "two commits"

The brief named `801b762` and `8831e94` as two things to deploy. My first ancestry check
asked "is 801b762 an ancestor of 8831e94" — false — and I nearly reported a problem.
The truth is the reverse: **`8831e94` is the PARENT, `801b762` is the tip**, so deploying
`801b762` alone carries both. Check ancestry in BOTH directions, or a one-commit deploy
looks like a missing-commit incident.

## The `for_chat` fix works — and there is a SECOND cap behind it

`search_leads(for_chat=...)` makes the offer-a-spreadsheet branch chat-only, so
`lead_digest.render` (which passes `for_chat=False`) now returns real leads instead of the
93-character question. Measured:

| | before | after |
|---|---|---|
| TX/school/SVPP | 93 chars, 0 leads | **4,450 bytes, 15 leads** |
| TX/school/NSGP | 93 chars, 0 leads | **3,632 bytes, 15 leads** |

**But a separate display cap of 15 rows is still in force, and `limit` does not move it** —
`limit=50` and `limit=100` both yield exactly 15 leads and byte-identical output. So the
email carries 15 of 81 (SVPP) and 15 of 18 (NSGP).

**That was safe to send only because the body says so itself:**
`Showing 15 of 81 matches — refine the search or export all results.`
An undisclosed partial would have been a false claim; a disclosed one is a true, useful,
incomplete answer. That distinction is the whole reason the send went ahead.

**Residual chat-ism, worth fixing next:** that disclosure line ends *"refine the search or
export all results"* — an instruction the recipient cannot act on from an inbox. It is the
same class of leak `for_chat` was introduced to fix, one string further down. The
destination is now explicit for the big branch and still implicit for the trailer.

## The send — first real lead email to a rep

Recipient **resolved through the reviewed roster** (`resend_client.recipient_for("U01E908206M")`
→ `kerry@monarchconnected.com`), never from a caller-supplied string; `send_to_rep` takes a
Slack id and no parameter anywhere accepts an address. Two emails, because `program` is a
single-valued spec key:

- `Texas schools — SVPP grant leads` — 4,450 B, 15 leads listed, header "Found 81"
- `Texas schools — NSGP grant leads` — 3,632 B, 15 leads listed, header "Found 18"

Each body re-checked immediately before its own send (asks-a-question / `<model-note>` /
"Nothing new in" opener / zero leads) with the send refused on any failure. Both returned
`Sent it to kerry@monarchconnected.com.`

Slack reply posted once in her 23 July thread, ts `1786397475.119959`, deliberately naming
the partial rather than implying the full list:
> Sent to kerry@monarchconnected.com — two emails, SVPP and NSGP for Texas schools. Each
> lists the top 15 and says the full count, 81 and 18.

## Templating a target-specific helper is the same mistake wearing a hat

Recorded verbatim at the coordinator's request. Last deploy I built the script by
`sed`-templating the previous one; the substitution produced
`grant_watch/enrich/slack/grant.py`, `tar` failed, and the rollback tarball shipped without
`grant.py`. [[deploy-mechanism]] already says never REUSE a target-specific helper —
templating one is the same mistake wearing a hat. This deploy's script was written fresh
and **asserts its own rollback tar member count (5) before proceeding**, so the same
failure cannot pass silently again.

## `filter_by_application_status` — never arm this one

Kerry's "school systems in Texas that have applied for the COPS grant". Grant holds
funding opportunities, solicitations and verified awards. **Application status exists in no
source Grant has**, so arming it would make Grant promise data that does not exist
anywhere — a promise no code change can keep, because the fix would have to be a new data
source, not a tool. Distinct from the "middle tier" gaps, which were real capabilities
merely lacking a Slack tool (now closed for two of them by `zoominfo_fill_many`).

Related: [[email-results-cannot-send-a-long-list]], [[deploy-mechanism]],
[[session-end-state-20260810]].
