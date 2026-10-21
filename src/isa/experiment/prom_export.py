"""Prometheus range exports: the run's signals and controller state, as CSV.

Long format, one row per sample: ts, series (its labels), value. A run
directory then holds the same data the dashboard showed, without needing
Prometheus to still be running, or to still have the data.
"""

import csv
from pathlib import Path

import httpx

EXPORTS = {
    "ttft_p95": "isa:ttft:p95_30s",
    "ttft_p99": "isa:ttft:p99_30s",
    "router_ttft_p95": "isa:router_ttft:p95_30s",
    "waiting": "isa:waiting:sum",
    "running": "isa:running:sum",
    "queue_slope": "isa:waiting:slope30s",
    "kv_usage_max": "isa:kv_usage:max",
    "gpu_util": "isa:gpu_util:avg",
    "arrival_rps": "isa:arrival:rps30s",
    "router_inflight": "router_inflight",
    "controller_replicas": "controller_replicas",
    "controller_desired": "controller_desired_replicas",
    "controller_target": "controller_target_replicas",
}


def export_range(
    client: httpx.Client, base_url: str, expr: str, start: float, end: float, step_s: float
) -> list[dict]:
    r = client.get(
        f"{base_url.rstrip('/')}/api/v1/query_range",
        params={"query": expr, "start": start, "end": end, "step": step_s},
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"range query {expr!r} failed: {body.get('error')}")
    rows: list[dict] = []
    for series in body["data"]["result"]:
        labels = {k: v for k, v in series["metric"].items() if k != "__name__"}
        key = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        for ts, value in series["values"]:
            rows.append({"ts": float(ts), "series": key, "value": float(value)})
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "series", "value"])
        writer.writeheader()
        writer.writerows(rows)