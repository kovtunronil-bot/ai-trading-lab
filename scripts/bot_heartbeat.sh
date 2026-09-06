#!/usr/bin/env bash
set -euo pipefail
# Shared heartbeat check for Bot Guardian workflows.
# Usage: bash scripts/bot_heartbeat.sh "<workflow name to dispatch>"
# Exits 0 either way; dispatches the target workflow only when the bot's last
# heartbeat (latest commit touching its live-storage files) is older than 45min.
LAST=$(git log -1 --format=%ct -- lab.db state.json proposals.json journal.csv 2>/dev/null)
LAST=${LAST:-0}
NOW=$(date +%s)
AGE_MIN=$(( (NOW - LAST) / 60 ))
echo "bot last heartbeat ${AGE_MIN} min ago"
if [ "$AGE_MIN" -gt 45 ]; then
  echo "heartbeat COLD -> dispatching: $1"
  gh workflow run "$1" --repo "$GITHUB_REPOSITORY" --ref master
else
  echo "heartbeat fresh -> cadence is healthy, nothing to do"
fi