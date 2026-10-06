"""Signals: the controller's typed view of the system at one tick.

Built by the metrics source (step 10) from Prometheus; consumed by policies and
the stabilizer, which are pure functions of it.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Signals:
    ts: float
    ready: int
    starting: int
    draining: int
    waiting_total: float
    running_total: float
    kv_usage_max: float  # worst replica: one replica near full preempts even if the mean is fine
    queue_slope_per_s: float
    gpu_util_mean: float
    ttft_p95_s: float | None = None
    arrival_rate_rps: float | None = None  # None when the router isn't being scraped
    stale: bool = False

    @property
    def current(self) -> int:
        """Capacity already committed: ready plus still-starting replicas.

        Counting STARTING is what prevents scale-up overshoot: while a new replica
        boots, the queue is still high, and a controller that compared only against
        READY would keep adding replicas for a problem it has already fixed.
        """
        return self.ready + self.starting

    @property
    def outstanding(self) -> float:
        """Requests in the system, running plus waiting: L in Little's law."""
        return self.running_total + self.waiting_total