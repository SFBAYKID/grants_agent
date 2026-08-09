---
name: deployed-vs-local-drift-20260809
description: Production is byte-exact at 90f0420 (stamp is truthful, verified by 90-file hash audit); 8 commits from 2026-08-09 are undeployed incl. a security fix and the CompletedPaidCall fix; ZoomInfo creds are on the droplet but its CODE never shipped
metadata:
  type: project
---

**Production tree == commit `90f0420` exactly.** Verified 2026-08-09: sha256 of all 90 deployed
`grant_watch/**/*.py` compared against `git show 90f0420:<path>` → **90/90 match, 0 mismatch**.
`.deployed_revision` is therefore TRUTHFUL, and the surgical `--files-from` deploys of 08-06 did NOT
leave a half-updated tree. Do not repeat the earlier suspicion that the stamp lies.

**8 commits sit undeployed** (all authored 2026-08-09, branch `review/rich-award-card-campaign-20260723`,
local HEAD `fcb1537`):
`e074b62` zoominfo transport → `3adebba` re-enriched lead reports outcome instead of "error" →
`d1a83ff` tool schema stops teaching a capability that doesn't exist → `bb4e0c9` **security: a web
page can no longer mint a Salesforce approval button** → `ca94286` phone numbers stop being
attributed to people who don't own them → `281c65e` docs → `35492ae` zoominfo ledger →
`fcb1537` zoominfo quote-then-spend.

Consequences live in production RIGHT NOW:
- `grant_watch/slack/contact_enrichment.py` on the droplet is **180 lines with NO
  `except paid_calls.CompletedPaidCall`** clause (local HEAD is 259 lines and has it at line 129,
  added by `3adebba`). So the user-visible contact-search crash in
  [[conversation-audit-20260809]] is UNFIXED in prod.
- The `bb4e0c9` security fix is undeployed.
- **ZoomInfo: the CREDENTIALS are on the droplet but the CODE is not.** `zoominfo.py`,
  `zoominfo_credits.py`, `zoominfo_enrichment.py`, `migrations_zoominfo.py` are LOCAL-ONLY —
  never deployed. That is the real reason `grep -rI ZOOMINFO` over the droplet returns zero files
  (see [[env-zoominfo-20260809]]); it is not that the feature is disabled by a flag.
- 6 local-only files total; the other two (`slack/search_enrichment.py`, `slack/tool_schemas.py`)
  are the `tools.py` split — deployed `tools.py` is 920 lines, local is 689.

**Method worth reusing (and the trap that nearly produced a false report):** build the file list on
the droplet with `find … -name '*.py'`, hash both sides, `join` on path. My first attempt ran
`git show` from the SCRATCHPAD directory, not the repo — git failed silently, every "expected" hash
became the sha256 of the empty string (`e3b0c442…`), and the audit reported **90/90 MISMATCH**. Always
assert `git rev-parse --show-toplevel` first and count how many hashes came back empty; a 100%
mismatch is far more likely to be a broken harness than a broken deploy. Same family as the
0-files-compared "PASS" in [[deploy-d66802b-card-comma]].
