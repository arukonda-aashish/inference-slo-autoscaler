"""Router metrics.

Per-replica gauges come from a custom collector that reads the registry at
scrape time: always current, and a removed replica's series disappears instead
of lingering as a stale label.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from isa.common.buckets import TTFT_BUCKETS
from isa.router.registry import Registry, ReplicaState


class _ReplicaCollector(Collector):
    def __init__(self, registry: Registry) -> None:
        self._registry = registry

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


class RouterMetrics:
    def __init__(self, registry: Registry) -> None:
        self.registry = CollectorRegistry()
        self.registry.register(_ReplicaCollector(registry))
        self.requests = Counter(
            "router_requests", "Requests by response status", ["code"], registry=self.registry
        )
        self.upstream_errors = Counter(
            "router_upstream_errors",
            "Connect or read failures talking to a replica",
            ["replica"],
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