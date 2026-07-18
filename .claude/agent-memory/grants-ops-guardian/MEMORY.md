# Guardian memory index (grants-ops-guardian)

- [Tenant + layout](tenant-and-layout.md) — grantwatch user, home, repo/venv paths, DB name/role, bot manager, cron jobs
- [Deploy mechanism + gotchas](deploy-mechanism.md) — proven rsync recipe; zsh `:gr` destination trap (brace ${h}!); marker ground-truth check; broken .venv/bin/pip
- [macOS archive safety](macos-archive-safety.md) — avoid Bash `mapfile`; fail closed before `git archive` so an empty delta cannot expand to the full tracked tree
- [Google Sheets export verify](google-sheets-export-verify.md) — droplet Drive export wiring verified 2026-07-14; reusable create+trash smoke-test recipe
- [Silent LLM fallback](grant-bot-silent-llm-fallback.md) — bot.log logs NOTHING on LLM or tool failures (stderr proven to land there); mtime-vs-lstart proves it
- [Tenant DB write safety](tenant-db-write-safety.md) — back up .db+wal+shm as a set; guarded BEGIN IMMEDIATE + rowcount==1 assert for live-DB row fixes; crm_actions/crm_action_items schema
- [Salesforce read-only describe](salesforce-readonly-describe.md) — `_readonly_get` can hit describe/global-describe; secret-safe sandbox-confirm booleans; Lead record-type default trap (Verkada is default, not the one named DeveloperName=Default)
- [Salesforce writer FLS](salesforce-writer-fls.md) — writer app creates Lead/Task/Note in monarchdev sandbox, ALL new fields persist (no FLS drop); Verkada record-type id; synthetic probe record ids
- [Migration version collision](migration-version-collision.md) — droplet DB carries SIDE-lineage migration numbering; main's migration 9 (org_* cols) is masked/never applied; verify schema not just "no migration error"
- [ContentNote link bug](salesforce-contentnote-link-bug.md) — create_content_note inserts the note but its link-lookup SOQL 400s in monarchdev, leaving the note unattached; note.Id already == ContentDocumentId; auto author-link gotcha
- [Stop means stop](coordinator-stop-is-stop.md) — a classifier block or coordinator stop halts the whole mutating effort; never finish the goal via an alternate allowed path
