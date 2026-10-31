#!/usr/bin/env bash
# Background services on the pod: Prometheus (:9090) and the GPU exporter (:9400).
set -euo pipefail
mkdir -p logs deploy/prometheus/targets /tmp/prom
[[ -f deploy/prometheus/targets/replicas.json ]] || echo "[]" > deploy/prometheus/targets/replicas.json

nohup prometheus --config.file=deploy/prometheus/prometheus.yml --storage.tsdb.path=/tmp/prom \
  --web.listen-address=127.0.0.1:9090 --web.enable-lifecycle > logs/prometheus.log 2>&1 &
nohup .venv/bin/python -m isa.gpu_exporter --port 9400 > logs/gpu_exporter.log 2>&1 &
sleep 3

echo "--- prometheus:"; curl -s 127.0.0.1:9090/-/ready; echo
echo "--- gpu exporter:"; curl -s 127.0.0.1:9400/metrics | grep '^isa_gpu'