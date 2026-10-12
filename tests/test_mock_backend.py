import asyncio
import socket
from pathlib import Path

import httpx

from isa.controller.backends.mock import MockBackend, MockBackendConfig
from isa.controller.health import probe, warmup

CONFIGS = Path(__file__).resolve().parent.parent / "configs"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_log(path: Path) -> str:
    return path.read_text()


def backend(tmp_path: Path, cold_start_s: float) -> MockBackend:
    return MockBackend(
        MockBackendConfig(
            cold_start_s=cold_start_s,
            log_dir=str(tmp_path),
            engine_config=str(CONFIGS / "mock_engine.yaml"),
            metrics_map=str(CONFIGS / "metrics_map.yaml"),
            model_name=MODEL,
        )
    )


async def test_spawn_probe_warmup_terminate(tmp_path):
    b = backend(tmp_path, cold_start_s=0.5)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    handle = await b.spawn("t1", port)
    try:
        async with httpx.AsyncClient() as client:
            assert await probe(client, url) is False  # still starting: not listening
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 15
            while not await probe(client, url):
                assert loop.time() < deadline, read_log(tmp_path / "t1.log")
                await asyncio.sleep(0.2)
            assert await warmup(client, url, MODEL)
        assert b.is_alive(handle)
    finally:
        await b.terminate(handle, grace_s=5)
    assert not b.is_alive(handle)
    assert "replica_ready" in read_log(tmp_path / "t1.log")


async def test_terminate_during_cold_start(tmp_path):
    b = backend(tmp_path, cold_start_s=30)
    handle = await b.spawn("t2", free_port())
    await asyncio.sleep(0.5)
    await b.terminate(handle, grace_s=5)
    assert not b.is_alive(handle)