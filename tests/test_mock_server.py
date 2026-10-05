import asyncio
import json
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

from isa.common.config import MetricsMap, load_yaml
from isa.mock_replica.engine import EngineConfig
from isa.mock_replica.server import ServerConfig, create_app

CONFIGS = Path(__file__).resolve().parent.parent / "configs"
MODEL = "test-model"
NAMES = load_yaml(CONFIGS / "metrics_map.yaml", MetricsMap)

# ~1ms steps so tests run fast with real sleeps.
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


@pytest.fixture
async def env():
    app = create_app(
        ServerConfig(replica_id="t1", model_name=MODEL, port=9999), FAST_ENGINE, NAMES
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


def prompt(n: int) -> str:
    return " ".join(["word"] * n)


def parse_sse(text: str) -> list:
    events = []
    for block in text.strip().split("\n\n"):
        assert block.startswith("data: "), block
        payload = block.removeprefix("data: ")
        events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


async def complete(client: httpx.AsyncClient, **body) -> httpx.Response:
    return await client.post("/v1/completions", json={"model": MODEL, **body})


# ---- health ------------------------------------------------------------------


async def test_health_ok(env):
    client, _ = env
    assert (await client.get("/health")).status_code == 200


async def test_health_503_when_driver_dead(env):
    client, app = env
    task = app.state.driver._task
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert (await client.get("/health")).status_code == 503


# ---- completions ---------------------------------------------------------------


async def test_nonstream_completion(env):
    client, _ = env
    r = await complete(client, prompt=prompt(20), max_tokens=5)
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["text"] == " tok" * 5
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["usage"] == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}


async def test_stream_completion(env):
    client, _ = env
    r = await complete(client, prompt=prompt(10), max_tokens=6, stream=True)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[-1] == "[DONE]"
    chunks = events[:-1]
    assert [c["choices"][0]["text"] for c in chunks] == [" tok"] * 6
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None] * 5 + ["length"]


async def test_stream_include_usage(env):
    client, _ = env
    r = await complete(
        client, prompt=prompt(7), max_tokens=4, stream=True,
        stream_options={"include_usage": True},
    )
    events = parse_sse(r.text)
    usage_chunk = events[-2]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}


async def test_unknown_openai_fields_ignored(env):
    client, _ = env
    r = await complete(client, prompt=prompt(5), max_tokens=2, temperature=0.7, top_p=0.9)
    assert r.status_code == 200


async def test_wrong_model_404(env):
    client, _ = env
    r = await client.post(
        "/v1/completions", json={"model": "other", "prompt": "hi", "max_tokens": 2}
    )
    assert r.status_code == 404


async def test_request_exceeding_model_len_400(env):
    client, _ = env
    r = await complete(client, prompt=prompt(1000), max_tokens=100)
    assert r.status_code == 400
    assert "max_model_len" in r.json()["message"]


async def test_empty_prompt_400(env):
    client, _ = env
    r = await complete(client, prompt="", max_tokens=5)
    assert r.status_code == 400


async def test_concurrent_requests_all_complete(env):
    client, _ = env

    async def one(i: int) -> dict:
        r = await complete(client, prompt=prompt(10 + i), max_tokens=8)
        return r.json()["usage"]

    # 12 > max_num_seqs=8, so some requests queue before admission.
    usages = await asyncio.gather(*(one(i) for i in range(12)))
    assert all(u["completion_tokens"] == 8 for u in usages)
    assert [u["prompt_tokens"] for u in usages] == [10 + i for i in range(12)]


# ---- metrics and abort -------------------------------------------------------


async def test_metrics_exposes_mapped_names(env):
    client, _ = env
    await complete(client, prompt=prompt(10), max_tokens=3)
    text = (await client.get("/metrics")).text
    expected = (NAMES.waiting, NAMES.running, NAMES.kv_usage, NAMES.ttft_bucket, NAMES.tpot_bucket)
    for name in expected:
        assert name in text, name
    assert f'{NAMES.ttft_hist}_count{{model_name="{MODEL}"}} 1.0' in text
    assert 'isa_gpu_utilization_percent{gpu="0"} 0.0' in text  # idle after completion


async def test_driver_abort_frees_kv(env):
    _, app = env
    driver = app.state.driver
    driver.submit("x", prompt_tokens=50, max_tokens=500)
    await asyncio.sleep(0.02)
    assert driver.engine.kv_usage > 0
    driver.abort("x")
    assert driver.engine.kv_usage == 0.0