"""Prometheus metrics for the mock replica, named per configs/metrics_map.yaml.

Uses a per-app CollectorRegistry rather than the global default, so tests can
create several apps in one process without duplicate-metric errors.
"""

import random

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from isa.common.config import MetricsMap
from isa.mock_replica.engine import Engine, StepResult

# Fine-grained at the low end: if every TTFT lands in one bucket, percentiles are fiction.
TTFT_BUCKETS = (
    0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.15, 0.25, 0.5, 0.75,
    1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 60.0,
)
TPOT_BUCKETS = (
    0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.075,
    0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0,
)


class MockMetrics:
    def __init__(self, names: MetricsMap, model_name: str, engine: Engine) -> None:
        self.registry = CollectorRegistry()
        labels = ["model_name"]

        # Gauges read engine state at scrape time, so they're never stale.
        def gauge(name: str, doc: str, fn) -> None:
            g = Gauge(name, doc, labels, registry=self.registry)
            g.labels(model_name=model_name).set_function(fn)

        gauge(names.waiting, "Requests waiting for admission", lambda: engine.num_waiting)
        gauge(names.running, "Requests admitted and running", lambda: engine.num_running)
        gauge(names.kv_usage, "Fraction of KV cache blocks in use", lambda: engine.kv_usage)

        # The baseline policy's input. Mimics a real GPU: pinned high whenever
        # anything runs, regardless of how loaded the replica actually is.
        gpu = Gauge(
            "isa_gpu_utilization_percent",
            "Simulated GPU utilization",
            ["gpu"],
            registry=self.registry,
        )
        gpu.labels(gpu="0").set_function(
            lambda: random.uniform(96.0, 99.0) if engine.num_running else 0.0
        )

        self._ttft = Histogram(
            names.ttft_hist, "Time to first token", labels,
            buckets=TTFT_BUCKETS, registry=self.registry,
        ).labels(model_name=model_name)
        self._tpot = Histogram(
            names.tpot_hist, "Mean time per output token, per request", labels,
            buckets=TPOT_BUCKETS, registry=self.registry,
        ).labels(model_name=model_name)
        self._preemptions = Counter(
            "isa_mock_preemptions", "Requests preempted for KV pressure", labels,
            registry=self.registry,
        ).labels(model_name=model_name)

    def on_step(self, result: StepResult) -> None:
        for _, ttft in result.first_token_ttfts:
            self._ttft.observe(ttft)
        for rec in result.finished:
            if rec.tpot_s is not None:
                self._tpot.observe(rec.tpot_s)
        if result.preempted:
            self._preemptions.inc(len(result.preempted))

    def render(self) -> bytes:
        return generate_latest(self.registry)