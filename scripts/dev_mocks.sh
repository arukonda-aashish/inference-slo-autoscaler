#!/usr/bin/env bash
# Start N mock replicas on ports 8001.. for local development. Ctrl-C stops all.
# Also writes Prometheus file_sd targets so the replicas are scraped automatically.
# Usage: scripts/dev_mocks.sh [N]   (default 3; logs in logs/mock-rN.log)
set -euo pipefail

N="${1:-3}"
TARGETS="deploy/prometheus/targets/replicas.json"
mkdir -p logs "$(dirname "$TARGETS")"
pids=()

write_targets() {
  # Atomic: write a temp file, then rename, so Prometheus never reads a partial file.
  local n="$1" body="[]" i
  if (( n > 0 )); then
    body="["
    for (( i = 1; i <= n; i++ )); do
      (( i > 1 )) && body+=","
      body+="{\"targets\":[\"127.0.0.1:$((8000 + i))\"],\"labels\":{\"replica\":\"r$i\"}}"
    done
    body+="]"
  fi
  echo "$body" > "$TARGETS.tmp"
  mv "$TARGETS.tmp" "$TARGETS"
}

cleanup() {
  kill "${pids[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
  write_targets 0
  echo "stopped ${#pids[@]} replicas"
}
trap cleanup INT TERM

for (( i = 1; i <= N; i++ )); do
  port=$((8000 + i))
  # .venv/bin/python directly, not `uv run`, so kill reaches the server process itself.
  .venv/bin/python -m isa.mock_replica --replica-id "r$i" --port "$port" \
    > "logs/mock-r$i.log" 2>&1 &
  pids+=($!)
  echo "r$i -> http://127.0.0.1:$port  (logs/mock-r$i.log)"
done

write_targets "$N"
echo "prometheus targets -> $TARGETS"

wait