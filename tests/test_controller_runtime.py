import asyncio
import json
from pathlib import Path

import httpx

from isa.common.config import MetricsMap, load_yaml
from isa.controller.api import create_app
from isa.controller.backends.mock import MockBackend, MockBackendConfig
from isa.controller.config import ControllerConfig
from isa.controller.decision_log import DecisionLog
from isa.controller.lifecycle import Phase
from isa.controller.metrics_source import Observed
from isa.controller.policies import QueuePolicy, QueuePolicyConfig
from isa.controller.runtime import Controller
from isa.controller.stabilizer import Stabilizer, StabilizerConfig

CONFIGS = Path(__file__).resolve().parent.parent / "configs"
NAMES = load_yaml(CONFIGS / "metrics_map.yaml", MetricsMap)
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


class FakeSource:
    """Starts stale, as real Prometheus is when no replica exists yet."""

    def __init__(self) -> None:
        self.obs = Observed({}, stale=True, stale_reason="no replicas yet")

    def set_load(self, outstanding: float) -> None:
        values = {
            "waiting_total": 0.0,
            "running_total": float(outstanding),
            "kv_usage_max": 0.1,
            "queue_slope_per_s": 0.0,
            "gpu_util_mean": 0.0,
            "ttft_p95_s": None,
            "arrival_rate_rps": None,
        }
        self.obs = Observed(values, stale=False, age_s=1.0)

    async def observe(self) -> Observed:
        return self.obs


def make_controller(tmp_path: Path, port_base: int, source: FakeSource) -> Controller:
    cfg = ControllerConfig(
        tick_s=0.2,
        health_interval_s=0.1,
        startup_timeout_s=20,
        drain_router_grace_s=0.1,
        drain_timeout_s=10,
        terminate_grace_s=3,
        port_base=port_base,
        port_count=4,
        targets_path=str(tmp_path / "targets.json"),
        backend=MockBackendConfig(
            cold_start_s=0.3,
            log_dir=str(tmp_path / "logs"),
            engine_config=str(CONFIGS / "mock_engine.yaml"),
            metrics_map=str(CONFIGS / "metrics_map.yaml"),
            model_name=MODEL,
        ),
    )
    return Controller(
        cfg,
        policy=QueuePolicy(QueuePolicyConfig(kind="queue", target_concurrency=10)),
        stabilizer=Stabilizer(
            StabilizerConfig(
                min_replicas=1, max_replicas=3, max_step_up=2, up_cooldown_s=0, down_window_s=0.5
            )
        ),
        backend=MockBackend(cfg.backend),
        source=source,
        names=NAMES,
        decision_log=DecisionLog(tmp_path / "decisions.jsonl"),
        client=httpx.AsyncClient(),
        model_name=MODEL,
        close_client=True,
    )


async def wait_until(cond, what: str = "", within_s: float = 20.0) -> None:
    try:
        async with asyncio.timeout(within_s):
            # Polling is deliberate: we wait on plain controller state that
            # production code updates without signalling, so there's no event to await.
            while not cond():  # noqa: ASYNC110
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise AssertionError(f"timed out after {within_s}s waiting for {what}") from None


def read_json(path: Path):
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def ready_ids(controller: Controller) -> set[str]:
    return {r.replica_id for r in controller.pool.in_phase(Phase.READY)}


async def test_restores_min_while_stale_serves_api_and_replaces_a_crash(tmp_path):
    controller = make_controller(tmp_path, 18101, FakeSource())
    app = create_app(controller)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://controller") as api:
            await wait_until(lambda: ready_ids(controller) == {"r1"}, what="r1 ready")
            assert (await api.get("/replicas")).json() == [
                {"replica_id": "r1", "url": "http://127.0.0.1:18101", "state": "ready"}
            ]
            assert 'controller_replicas{phase="ready"} 1.0' in (await api.get("/metrics")).text
            assert read_json(tmp_path / "targets.json")[0]["labels"]["replica"] == "r1"

            controller.pool.get("r1").handle.process.kill()  # simulate a crash
            await wait_until(lambda: ready_ids(controller) == {"r2"}, what="replacement r2 ready")

    records = read_jsonl(tmp_path / "decisions.jsonl")
    first_tick = next(r for r in records if r["type"] == "tick")
    assert first_tick["signals"]["stale"] is True
    assert first_tick["action"]["reason"] == "restore_min"  # D23, live
    events = {(r["replica_id"], r["to"], r["reason"]) for r in records if r["type"] == "lifecycle"}
    assert ("r1", "failed", "process_exited") in events
    assert ("r2", "ready", "warmup_ok") in events
    assert read_json(tmp_path / "targets.json") == []  # stop() cleared the targets


async def test_scales_up_on_load_then_drains_down_one_at_a_time(tmp_path):
    source = FakeSource()
    controller = make_controller(tmp_path, 18201, source)
    await controller.start()
    try:
        await wait_until(lambda: len(ready_ids(controller)) == 1, what="initial replica")
        source.set_load(25)  # ceil(25 / 10) = 3
        await wait_until(lambda: len(ready_ids(controller)) == 3, what="scale up to 3")
        source.set_load(0)
        await wait_until(
            lambda: len(ready_ids(controller)) == 1
            and controller.pool.count(Phase.DRAINING) == 0
            and not controller._busy,
            within_s=30,
            what="drain back to 1",
        )
    finally:
        await controller.stop()

    records = read_jsonl(tmp_path / "decisions.jsonl")
    acted = [r["action"]["reason"] for r in records if r["type"] == "tick" and r["action"]["delta"]]
    assert acted[:2] == ["restore_min", "scale_up"]
    assert acted.count("scale_down") == 2
    terminated = [r for r in records if r["type"] == "lifecycle" and r["to"] == "terminated"]
    assert [r["reason"] for r in terminated] == ["drained", "drained"]
    # One drain at a time: the stabilizer holds while a replica is draining.
    assert any(
        r["type"] == "tick" and r["action"]["reason"] == "hold_transition_in_progress"
        for r in records
    )