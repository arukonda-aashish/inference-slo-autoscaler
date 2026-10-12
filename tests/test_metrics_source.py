from pathlib import Path

import httpx
import yaml

from isa.controller.metrics_source import (
    AGE_QUERY,
    OPTIONAL,
    REQUIRED,
    MetricsSource,
    build_signals,
)

ROOT = Path(__file__).resolve().parent.parent

HEALTHY = {
    "isa:waiting:sum": 3.0,
    "isa:running:sum": 12.0,
    "isa:kv_usage:max": 0.4,
    "isa:waiting:slope30s": 0.5,
    "isa:gpu_util:avg": 97.0,
    "isa:ttft:p95_30s": 0.12,
    "isa:arrival:rps30s": 8.0,
    AGE_QUERY: 1.5,
}


def fake_prometheus(values: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        v = values.get(request.url.params["query"])
        result = [] if v is None else [{"metric": {}, "value": [1.7e9, str(v)]}]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
        )

    return httpx.MockTransport(handler)


async def observe(values: dict | None = None, transport=None):
    transport = transport or fake_prometheus(values)
    async with httpx.AsyncClient(transport=transport) as client:
        return await MetricsSource("http://prom", client, max_age_s=10).observe()


async def test_fresh_observation():
    obs = await observe(HEALTHY)
    assert not obs.stale
    assert obs.values["waiting_total"] == 3.0
    assert obs.values["arrival_rate_rps"] == 8.0
    assert obs.age_s == 1.5


async def test_missing_required_series_is_stale():
    obs = await observe({k: v for k, v in HEALTHY.items() if k != "isa:running:sum"})
    assert obs.stale
    assert "running_total" in obs.stale_reason


async def test_old_data_is_stale():
    obs = await observe({**HEALTHY, AGE_QUERY: 45.0})
    assert obs.stale
    assert "data_too_old" in obs.stale_reason


async def test_nan_ttft_is_no_data_not_stale():
    obs = await observe({**HEALTHY, "isa:ttft:p95_30s": "NaN"})
    assert not obs.stale
    assert obs.values["ttft_p95_s"] is None


async def test_missing_router_is_not_stale():
    obs = await observe({k: v for k, v in HEALTHY.items() if k != "isa:arrival:rps30s"})
    assert not obs.stale
    assert obs.values["arrival_rate_rps"] is None


async def test_prometheus_down_is_stale():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    obs = await observe(transport=httpx.MockTransport(refuse))
    assert obs.stale
    assert "prometheus_unavailable" in obs.stale_reason


async def test_query_error_is_stale():
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "error": "parse error"})

    obs = await observe(transport=httpx.MockTransport(bad))
    assert obs.stale


async def test_build_signals_merges_controller_counts():
    obs = await observe(HEALTHY)
    s = build_signals(obs, ts=100.0, ready=2, starting=1, draining=0)
    assert (s.current, s.outstanding, s.stale) == (3, 15.0, False)


def test_queried_series_are_all_recorded_by_rules():
    doc = yaml.safe_load((ROOT / "deploy" / "prometheus" / "rules.yml").read_text())
    recorded = {r["record"] for g in doc["groups"] for r in g["rules"]}
    queried = set(REQUIRED.values()) | set(OPTIONAL.values())
    assert queried <= recorded, queried - recorded