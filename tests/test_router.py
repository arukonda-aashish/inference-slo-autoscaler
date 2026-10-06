import asyncio
import json
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx

from isa.common.config import MetricsMap, load_yaml
from isa.mock_replica.engine import EngineConfig
from isa.mock_replica.server import ServerConfig
from isa.mock_replica.server import create_app as create_mock_app
from isa.router.app import create_app as create_router_app
from isa.router.config import RouterConfig
from isa.router.registry import ReplicaSpec, ReplicaState

CONFIGS = Path(__file__).resolve().parent.parent / "configs"
MODEL = "test-model"
NAMES = load_yaml(CONFIGS / "metrics_map.yaml", MetricsMap)
FAST_ENGINE = EngineConfig(
    block_size=16,
    num_kv_blocks=256,
    watermark_frac=0.0,
    max_num_seqs=8,
    max_batched_tokens=512,
    max_model_len=1024,
    step_base_ms=1.0,
    step_per_seq_ms=0.1,
    prefill_per_token_ms=0.001,
)


class Refuse(httpx.AsyncBaseTransport):
    """A replica that refuses connections, like a crashed or still-loading server."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


@asynccontextmanager
async def router_env(live=("r1", "r2", "r3"), dead=(), balancer="least_outstanding"):
    async with AsyncExitStack() as stack:
        mounts: dict[str, httpx.AsyncBaseTransport] = {}
        for rid in live:
            mock = create_mock_app(
                ServerConfig(replica_id=rid, model_name=MODEL, port=9000), FAST_ENGINE, NAMES
            )
            await stack.enter_async_context(mock.router.lifespan_context(mock))
            mounts[f"http://{rid}"] = httpx.ASGITransport(app=mock)
        for rid in dead:
            mounts[f"http://{rid}"] = Refuse()
        upstream = await stack.enter_async_context(httpx.AsyncClient(mounts=mounts))

        specs = [ReplicaSpec(replica_id=r, url=f"http://{r}") for r in (*live, *dead)]
        router = create_router_app(
            RouterConfig(balancer=balancer, replicas=specs), http_client=upstream
        )
        await stack.enter_async_context(router.router.lifespan_context(router))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=router), base_url="http://router")
        )
        yield client, router

def body(n_words=10, max_tokens=5, **extra) -> dict:
    prompt = " ".join(["word"] * n_words)
    return {"model": MODEL, "prompt": prompt, "max_tokens": max_tokens, **extra}


def parse_sse(text: str) -> list:
    events = []
    for block in text.strip().split("\n\n"):
        payload = block.removeprefix("data: ")
        events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def total_inflight(router) -> int:
    return sum(r.inflight for r in router.state.registry.all())


# ---- relaying ----------------------------------------------------------------


async def test_nonstream_through_router():
    async with router_env() as (client, _):
        r = await client.post("/v1/completions", json=body(n_words=20, max_tokens=5))
        assert r.status_code == 200
        assert r.headers["x-replica-id"] in {"r1", "r2", "r3"}
        usage = r.json()["usage"]
        assert usage == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}


async def test_stream_through_router():
    async with router_env() as (client, _):
        r = await client.post("/v1/completions", json=body(max_tokens=5, stream=True))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        assert events[-1] == "[DONE]"
        assert len(events) == 6


async def test_upstream_error_passes_through():
    async with router_env() as (client, _):
        r = await client.post(
            "/v1/completions", json={"model": "other", "prompt": "hi", "max_tokens": 2}
        )
        assert r.status_code == 404
        assert "does not exist" in r.json()["message"]


async def test_stream_upstream_error_is_json_not_sse():
    async with router_env() as (client, _):
        req = body(n_words=1000, max_tokens=100, stream=True)
        r = await client.post("/v1/completions", json=req)
        assert r.status_code == 400
        assert "max_model_len" in r.json()["message"]


async def test_invalid_json_400():
    async with router_env() as (client, _):
        r = await client.post(
            "/v1/completions", content=b"not json", headers={"content-type": "application/json"}
        )
        assert r.status_code == 400


# ---- balancing ---------------------------------------------------------------


async def test_least_outstanding_spreads_sequential_requests():
    async with router_env() as (client, _):
        seen = Counter()
        for _ in range(9):
            r = await client.post("/v1/completions", json=body())
            seen[r.headers["x-replica-id"]] += 1
        assert seen == {"r1": 3, "r2": 3, "r3": 3}  # idle ties rotate


async def test_concurrent_requests_use_every_replica():
    async with router_env() as (client, _):
        rs = await asyncio.gather(
            *(client.post("/v1/completions", json=body(max_tokens=20)) for _ in range(12))
        )
        assert all(r.status_code == 200 for r in rs)
        assert {r.headers["x-replica-id"] for r in rs} == {"r1", "r2", "r3"}


async def test_draining_replica_excluded():
    async with router_env() as (client, router):
        router.state.registry.update([
            ReplicaSpec(replica_id="r1", url="http://r1", state=ReplicaState.DRAINING),
            ReplicaSpec(replica_id="r2", url="http://r2"),
            ReplicaSpec(replica_id="r3", url="http://r3"),
        ])
        ids = [(await client.post("/v1/completions", json=body())).headers["x-replica-id"]
               for _ in range(6)]
        assert "r1" not in ids


async def test_no_ready_replicas_503():
    async with router_env() as (client, router):
        router.state.registry.update([
            ReplicaSpec(replica_id=r, url=f"http://{r}", state=ReplicaState.DRAINING)
            for r in ("r1", "r2", "r3")
        ])
        r = await client.post("/v1/completions", json=body())
        assert r.status_code == 503


# ---- failure handling and the in-flight invariant ----------------------------


async def test_connect_failure_retries_another_replica():
    async with router_env(live=("r1",), dead=("d1",)) as (client, router):
        rs = [await client.post("/v1/completions", json=body()) for _ in range(6)]
        assert all(r.status_code == 200 for r in rs)
        assert {r.headers["x-replica-id"] for r in rs} == {"r1"}
        metrics = (await client.get("/metrics")).text
        assert 'router_upstream_errors_total{replica="d1"}' in metrics
        assert total_inflight(router) == 0


async def test_all_replicas_dead_502():
    async with router_env(live=(), dead=("d1", "d2")) as (client, router):
        r = await client.post("/v1/completions", json=body())
        assert r.status_code == 502
        assert total_inflight(router) == 0


async def test_inflight_returns_to_zero_after_mixed_traffic():
    async with router_env() as (client, router):
        await asyncio.gather(
            *(client.post("/v1/completions", json=body(stream=True)) for _ in range(5)),
            *(client.post("/v1/completions", json=body()) for _ in range(5)),
            client.post("/v1/completions", json=body(n_words=1000, max_tokens=100)),  # 400
            client.post("/v1/completions", json={"model": "x", "prompt": "a", "max_tokens": 1}),
        )
        assert total_inflight(router) == 0


async def test_metrics_endpoint():
    async with router_env() as (client, _):
        await client.post("/v1/completions", json=body(stream=True))
        text = (await client.get("/metrics")).text
        assert "router_arrivals_total 1.0" in text
        assert 'router_requests_total{code="200"} 1.0' in text
        assert "router_ttft_seconds_count 1.0" in text
        assert 'router_inflight{replica="r1"} 0.0' in text
        assert 'router_replica_ready{replica="r1"} 1.0' in text