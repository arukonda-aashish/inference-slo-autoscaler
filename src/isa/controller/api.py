"""Controller HTTP API: the router's registry source, state for humans, and metrics."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from isa.controller.lifecycle import Phase
from isa.controller.runtime import Controller


class _ControllerCollector(Collector):
    def __init__(self, controller: Controller) -> None:
        self._c = controller

    def collect(self):
        phases = GaugeMetricFamily(
            "controller_replicas", "Managed replicas by phase", labels=["phase"]
        )
        for p in (Phase.STARTING, Phase.READY, Phase.DRAINING):
            phases.add_metric([p.value], self._c.pool.count(p))
        yield phases
        yield GaugeMetricFamily(
            "controller_desired_replicas", "Policy's raw desired count, last tick",
            value=self._c.desired,
        )
        yield GaugeMetricFamily(
            "controller_target_replicas", "Stabilizer's target, last tick", value=self._c.target
        )
        outcomes = CounterMetricFamily(
            "controller_actions", "Stabilizer outcomes by reason", labels=["reason"]
        )
        for reason, n in self._c.outcomes.items():
            outcomes.add_metric([reason], n)
        yield outcomes


def create_app(controller: Controller) -> FastAPI:
    registry = CollectorRegistry()
    registry.register(_ControllerCollector(controller))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await controller.start()
        try:
            yield
        finally:
            await controller.stop()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> Response:
        return Response(status_code=200)

    @app.get("/replicas")
    async def replicas() -> list[dict]:
        """What the router polls. STARTING and DRAINING are included so the
        router knows about them; it only routes to READY."""
        return [spec.model_dump(mode="json") for spec in controller.pool.router_specs()]

    @app.get("/state")
    async def state() -> dict:
        return {
            "policy": controller.policy.name,
            "desired": controller.desired,
            "target": controller.target,
            "replicas": [
                {
                    "replica_id": r.replica_id,
                    "phase": r.phase.value,
                    "url": r.url,
                    "health_failures": r.health_failures,
                }
                for r in controller.pool.active()
            ],
            "last_tick": controller.last_tick,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app