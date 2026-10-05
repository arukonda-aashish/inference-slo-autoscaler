#!/usr/bin/env bash
# Start N mock replicas on ports 8001.. for local development. Ctrl-C stops all.
# Usage: scripts/dev_mocks.sh [N]   (default 3; logs in logs/mock-rN.log)
set -euo pipefail

N="${1:-3}"
mkdir -p logs
pids=()

cleanup() {
  kill "${pids[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "stopped ${#pids[@]} replicas"
}
trap cleanup INT TERM

for i in $(seq 1 "$N"); do
  port=$((8000 + i))
  # .venv/bin/python directly, not `uv run`, so kill reaches the server process itself.
  .venv/bin/python -m isa.mock_replica --replica-id "r$i" --port "$port" \
    > "logs/mock-r$i.log" 2>&1 &
  pids+=($!)
  echo "r$i -> http://127.0.0.1:$port  (logs/mock-r$i.log)"
done

wait