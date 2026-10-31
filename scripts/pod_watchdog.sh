#!/usr/bin/env bash
# Hard spending limit: terminate this pod after N hours, no matter what.
# Usage (on the pod, first thing): nohup scripts/pod_watchdog.sh 3 > /tmp/watchdog.log 2>&1 &
set -euo pipefail
HOURS="${1:-3}"
: "${RUNPOD_POD_ID:?RUNPOD_POD_ID is not set: not a RunPod pod?}"
command -v runpodctl > /dev/null || { echo "runpodctl not found"; exit 1; }
echo "watchdog: pod $RUNPOD_POD_ID will be removed at $(date -d "+${HOURS} hours")"
sleep "$((HOURS * 3600))"
runpodctl remove pod "$RUNPOD_POD_ID"