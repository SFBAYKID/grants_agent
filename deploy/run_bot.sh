#!/usr/bin/env bash
# Grant bot launcher (droplet tenant). Keepalive cron restarts it if it dies.
cd ~/grants_agent
pgrep -f "grant_watch[.]slack[.]grant" >/dev/null && exit 0
set -a; source .env; set +a
nohup .venv/bin/python -u -m grant_watch.slack.grant >> bot.log 2>&1 &
