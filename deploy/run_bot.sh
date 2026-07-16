#!/usr/bin/env bash
# Grant bot launcher (droplet tenant). Keepalive cron restarts it if it dies.
cd ~/grants_agent
if pgrep -f "grant_watch[.]slack[.]grant" >/dev/null; then
  printf 'grant_keepalive status=healthy at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
else
  probe_status=$?
  if [[ "$probe_status" -ne 1 ]]; then
    printf 'grant_keepalive status=probe_error at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit "$probe_status"
  fi
fi
printf 'grant_keepalive status=restart_attempt at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set -a; source .env; set +a
nohup .venv/bin/python -u -m grant_watch.slack.grant >> bot.log 2>&1 &
