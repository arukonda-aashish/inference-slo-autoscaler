"""Replica lifecycle: the controller's source of truth for which replicas exist.

Pure state, no processes or I/O. The runtime (step 10b) drives transitions from
health checks and drain progress; this module only enforces that they're legal.

    STARTING --> READY --> DRAINING --> TERMINATED
        |          |           |
        +----------+-----------+------> FAILED

Replica ids are never reused (r1, r2, ...), so logs and Prometheus series for
different processes can't be confused. Ports are reused: a terminated replica's
process is gone by the time it reaches TERMINATED.
"""

from dataclasses import dataclass
from enum import StrEnum

from isa.router.registry import ReplicaSpec, ReplicaState


class Phase(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    TERMINATED = "terminated"
    FAILED = "failed"


TERMINAL = frozenset({Phase.TERMINATED, Phase.FAILED})

_ALLOWED: dict[Phase, frozenset[Phase]] = {
    Phase.STARTING: frozenset({Phase.READY, Phase.FAILED}),
    Phase.READY: frozenset({Phase.DRAINING, Phase.FAILED}),
    Phase.DRAINING: frozenset({Phase.TERMINATED, Phase.FAILED}),
}

# What the router sees. Terminal replicas are simply absent.
_ROUTER_STATE = {
    Phase.STARTING: ReplicaState.STARTING,
    Phase.READY: ReplicaState.READY,
    Phase.DRAINING: ReplicaState.DRAINING,
}


class TransitionError(RuntimeError):
    pass


@dataclass(eq=False)
class ManagedReplica:
    replica_id: str
    host: str
    port: int
    phase: Phase
    created_at: float
    changed_at: float
    health_failures: int = 0
    handle: object | None = None  # backend-specific, e.g. a process

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class Pool:
    def __init__(
        self, host: str = "127.0.0.1", port_base: int = 8001, port_count: int = 16
    ) -> None:
        self._host = host
        self._ports = range(port_base, port_base + port_count)
        self._active: dict[str, ManagedReplica] = {}
        self._next_id = 1
        self.history: list[tuple[float, str, Phase, Phase]] = []  # (ts, id, from, to)

    def active(self) -> list[ManagedReplica]:
        return list(self._active.values())

    def get(self, replica_id: str) -> ManagedReplica:
        return self._active[replica_id]

    def in_phase(self, *phases: Phase) -> list[ManagedReplica]:
        return [r for r in self._active.values() if r.phase in phases]

    def count(self, phase: Phase) -> int:
        return sum(r.phase is phase for r in self._active.values())

    def create(self, now: float) -> ManagedReplica:
        used = {r.port for r in self._active.values()}
        port = next((p for p in self._ports if p not in used), None)
        if port is None:
            raise RuntimeError(f"no free port in {self._ports.start}..{self._ports.stop - 1}")
        replica_id = f"r{self._next_id}"
        self._next_id += 1
        replica = ManagedReplica(replica_id, self._host, port, Phase.STARTING, now, now)
        self._active[replica_id] = replica
        return replica

    def transition(self, replica_id: str, to: Phase, now: float) -> ManagedReplica:
        replica = self._active[replica_id]
        if to not in _ALLOWED.get(replica.phase, frozenset()):
            raise TransitionError(f"{replica_id}: {replica.phase} -> {to} is not allowed")
        self.history.append((now, replica_id, replica.phase, to))
        replica.phase = to
        replica.changed_at = now
        if to in TERMINAL:
            del self._active[replica_id]
        return replica

    def router_specs(self) -> list[ReplicaSpec]:
        return [
            ReplicaSpec(replica_id=r.replica_id, url=r.url, state=_ROUTER_STATE[r.phase])
            for r in self._active.values()
        ]

    def scrape_targets(self) -> list[ManagedReplica]:
        """READY and DRAINING only. A STARTING replica isn't listening yet, and a
        DRAINING one is still serving in-flight requests whose metrics matter."""
        return self.in_phase(Phase.READY, Phase.DRAINING)