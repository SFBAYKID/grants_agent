#!/usr/bin/env bash
# Fail-closed rsync deploy for grants_agent (reconstructed 2026-07-18 from
# grants-ops-guardian/deploy-mechanism.md — braced-destination guard, -c
# checksum transfer, documented excludes, dry|real modes). Run under bash.
set -euo pipefail

u="grantwatch"
h="143.110.134.172"
dest="${u}@${h}:grants_agent/"
[[ "$dest" == grantwatch@*:grants_agent/ ]] || { echo "DEST GUARD FAILED: $dest" >&2; exit 1; }

mode="${1:-dry}"
case "$mode" in
  dry)  flags="-cain --delete" ;;  # --delete in dry too, so deletions preview
  real) flags="-cai --delete" ;;
  *) echo "usage: deploy_rsync.sh dry|real" >&2; exit 1 ;;
esac

excludes=(
  --exclude ".git" --exclude ".venv" --exclude ".env" --exclude ".env.*"
  --exclude "*.db" --exclude "*.db-*" --exclude "*.sqlite*"
  --exclude "bot.log" --exclude "cron.log" --exclude "nohup.out"
  --exclude "__pycache__" --exclude "*.pyc" --exclude ".pytest_cache"
  --exclude ".mypy_cache" --exclude ".ruff_cache"
  --exclude ".deployed_revision" --exclude ".claude" --exclude ".codex"
  --exclude "secrets" --exclude ".idea" --exclude ".DS_Store"
  --exclude ".*.lock" --exclude "/run_bot.sh" --exclude "/deploy_rsync.sh"
  --exclude ".deploy_backup_*"
)

# shellcheck disable=SC2086
rsync $flags -e "ssh -i $HOME/.ssh/grants_droplet -o IdentitiesOnly=yes" \
  "${excludes[@]}" /Users/chasengonzales/grants_agent/ "$dest"
