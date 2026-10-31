"""Router metrics.

Per-replica gauges and the queue depth come from a custom collector that reads
current state at scrape time: always accurate, and a removed replica's series
disappears instead of lingering as a stale label.
"""

from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from isa.common.buckets import TTFT_BUCKETS
from isa.router.registry import Registry, ReplicaState


class _StateCollector(Collector):
    def __init__(self, registry: Registry, queue_depth: Callable[[], int]) -> None:
        self._registry = registry
        self._queue_depth = queue_depth

    def collect(self):
        inflight = GaugeMetricFamily(
            "router_inflight", "In-flight requests per replica", labels=["replica"]
        )
        ready = GaugeMetricFamily(
            "router_replica_ready", "1 if the replica is READY", labels=["replica"]
        )
        for r in self._registry.all():
            inflight.add_metric([r.replica_id], r.inflight)
            ready.add_metric([r.replica_id], 1.0 if r.state is ReplicaState.READY else 0.0)
        yield inflight
        yield ready
        yield GaugeMetricFamily(
            "router_queue_depth",
            "Requests waiting in the router for a replica slot (late binding)",
            value=self._queue_depth(),
        )


class RouterMetrics:
    def __init__(self, registry: Registry, queue_depth: Callable[[], int] = lambda: 0) -> None:
        self.registry = CollectorRegistry()
        self.registry.register(_StateCollector(registry, queue_depth))
        # Counted on receipt, before routing: offered load, including requests that
        # end up rejected or failed. router_requests counts responses instead.
        self.arrivals = Counter(
            "router_arrivals", "Requests received (offered load)", registry=self.registry
        )
        self.requests = Counter(
            "router_requests", "Requests by response status", ["code"], registry=self.registry
        )
        self.rejected = Counter(
            "router_rejected", "Requests shed by admission control", ["reason"],
            registry=self.registry,
        )
        self.upstream_errors = Counter(
            "router_upstream_errors",
            "Connect or read failures talking to a replica",
            ["replica"],
            registry=self.registry,
        )
        self.queue_wait = Histogram(
            "router_queue_wait_seconds",
            "Time a request waited in the router before getting a replica slot",
            buckets=TTFT_BUCKETS,
            registry=self.registry,
        )
        # Router-observed TTFT: request received -> first byte of a streamed
        # response. Compared against vLLM's own histogram, the gap is proxy overhead.
        self.ttft = Histogram(
            "router_ttft_seconds",
            "Time to first byte of a streamed response, as seen by the router",
            buckets=TTFT_BUCKETS,
            registry=self.registry,
        )