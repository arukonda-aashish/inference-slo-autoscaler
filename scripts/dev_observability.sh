#!/usr/bin/env bash
# Local Prometheus (:9090) and Grafana (:3000), configured entirely from this repo.
# Both bind to 127.0.0.1 only. Ctrl-C stops both.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
export ISA_REPO="$REPO"  # read by deploy/grafana/provisioning/dashboards/provider.yml

TARGETS="$REPO/deploy/prometheus/targets/replicas.json"
mkdir -p "$REPO/logs" "$REPO/deploy/prometheus/data" "$REPO/deploy/grafana/data" \
  "$(dirname "$TARGETS")"
[[ -f "$TARGETS" ]] || echo "[]" > "$TARGETS"

pids=()
cleanup() {
  kill "${pids[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "stopped prometheus and grafana"
}
trap cleanup INT TERM

prometheus \
  --config.file="$REPO/deploy/prometheus/prometheus.yml" \
  --storage.tsdb.path="$REPO/deploy/prometheus/data" \
  --storage.tsdb.retention.time=2d \
  --web.listen-address=127.0.0.1:9090 \
  --web.enable-lifecycle \
  > "$REPO/logs/prometheus.log" 2>&1 &
pids+=($!)

# Anonymous admin is acceptable only because Grafana is bound to localhost.
GF_PATHS_DATA="$REPO/deploy/grafana/data" \
GF_PATHS_PROVISIONING="$REPO/deploy/grafana/provisioning" \
GF_SERVER_HTTP_ADDR=127.0.0.1 \
GF_SERVER_HTTP_PORT=3000 \
GF_AUTH_ANONYMOUS_ENABLED=true \
GF_AUTH_ANONYMOUS_ORG_ROLE=Admin \
GF_AUTH_DISABLE_LOGIN_FORM=true \
GF_ANALYTICS_REPORTING_ENABLED=false \
GF_ANALYTICS_CHECK_FOR_UPDATES=false \
  grafana server --homepath "$(brew --prefix grafana)/share/grafana" \
  > "$REPO/logs/grafana.log" 2>&1 &
pids+=($!)

echo "prometheus -> http://127.0.0.1:9090  (logs/prometheus.log)"
echo "grafana    -> http://127.0.0.1:3000  (logs/grafana.log)"
wait