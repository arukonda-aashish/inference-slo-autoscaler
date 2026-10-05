"""Replica registry: which replicas exist, their state, and live in-flight counts.

The in-flight count is the router's own real-time view of load. Balancing (and
later admission control) read it directly and never wait on Prometheus.

The registry is fed by update(): from static config for now, and later by
polling the controller, which owns replica lifecycle.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from isa.common.config import StrictModel
from isa.common.log import get_logger

log = get_logger(__name__)


class ReplicaState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"


class ReplicaSpec(StrictModel):
    replica_id: str
    url: str
    state: ReplicaState = ReplicaState.READY


@dataclass(eq=False)
class Replica:
    replica_id: str
    url: str
    state: ReplicaState
    inflight: int = 0


class Registry:
    def __init__(self) -> None:
        self._replicas: dict[str, Replica] = {}

    def all(self) -> list[Replica]:
        return list(self._replicas.values())

    def ready(self) -> list[Replica]:
        return [r for r in self._replicas.values() if r.state is ReplicaState.READY]

    def get(self, replica_id: str) -> Replica | None:
        return self._replicas.get(replica_id)

    def update(self, specs: Iterable[ReplicaSpec]) -> None:
        """Make the registry match specs exactly.

        A replica removed while requests are in flight keeps serving them: each
        request holds its own reference to the Replica, so its decrement still
        lands on the right object even after the registry forgets it.
        """
        seen: set[str] = set()
        for spec in specs:
            seen.add(spec.replica_id)
            url = spec.url.rstrip("/")
            existing = self._replicas.get(spec.replica_id)
            if existing is None:
                self._replicas[spec.replica_id] = Replica(spec.replica_id, url, spec.state)
                log.info(
                    "replica_added", replica_id=spec.replica_id, url=url, state=spec.state.value
                )
                continue
            if existing.state is not spec.state:
                log.info(
                    "replica_state_changed",
                    replica_id=spec.replica_id,
                    old=existing.state.value,
                    new=spec.state.value,
                )
                existing.state = spec.state
            existing.url = url
        for replica_id in [rid for rid in self._replicas if rid not in seen]:
            removed = self._replicas.pop(replica_id)
            log.info("replica_removed", replica_id=replica_id, inflight=removed.inflight)