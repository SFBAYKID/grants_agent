# Guardian memory index (grants-ops-guardian)

- [Tenant + layout](tenant-and-layout.md) — grantwatch user, home, repo/venv paths, DB name/role, bot manager, cron jobs
- [Deploy mechanism + gotchas](deploy-mechanism.md) — droplet is NOT a git checkout (file-copy deploy via .deployed_revision); broken .venv/bin/pip wrapper; run_bot.sh keepalive
- [macOS archive safety](macos-archive-safety.md) — avoid Bash `mapfile`; fail closed before `git archive` so an empty delta cannot expand to the full tracked tree
- [Google Sheets export verify](google-sheets-export-verify.md) — droplet Drive export wiring verified 2026-07-14; reusable create+trash smoke-test recipe
