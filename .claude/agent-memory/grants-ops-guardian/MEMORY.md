# Guardian memory index (grants-ops-guardian)

- [Tenant + layout](tenant-and-layout.md) — grantwatch user, home, repo/venv paths, DB name/role, bot manager, cron jobs
- [Deploy mechanism + gotchas](deploy-mechanism.md) — proven rsync-with-excludes recipe (dry-run first; anchored /run_bot.sh + secrets excludes mandatory); broken .venv/bin/pip
- [macOS archive safety](macos-archive-safety.md) — avoid Bash `mapfile`; fail closed before `git archive` so an empty delta cannot expand to the full tracked tree
- [Google Sheets export verify](google-sheets-export-verify.md) — droplet Drive export wiring verified 2026-07-14; reusable create+trash smoke-test recipe
- [Silent LLM fallback](grant-bot-silent-llm-fallback.md) — bot.log logs NOTHING on LLM or tool failures (stderr proven to land there); mtime-vs-lstart proves it
