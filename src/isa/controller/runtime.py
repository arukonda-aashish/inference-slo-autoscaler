"""Controller runtime: the tick loop, the replica supervisor, and actuation.

Two loops share the pool under one lock:
  tick (every tick_s):
      observe -> signals -> policy -> stabilizer -> act -> decision log
  supervise (every health_interval_s):
      STARTING: process dead -> FAILED; startup timeout -> FAILED;
                health probe ok -> warmup -> READY
      READY:    process dead, or N consecutive failed probes -> FAILED
      DRAINING: after the router grace, once the replica itself reports zero in
                flight (or the drain times out) -> terminate -> TERMINATED

Slow operations (warmup, terminate) run as background tasks outside the lock,
so a minute-long vLLM warmup can't stall the tick loop.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

import httpx

from isa.common.config import MetricsMap
from isa.common.log import get_logger
from isa.controller.backends.base import ReplicaBackend
from isa.controller.config import ControllerConfig
from isa.controller.decision_log import DecisionLog
from isa.controller.health import probe, replica_load, warmup
from isa.controller.lifecycle import ManagedReplica, Phase, Pool
from isa.controller.metrics_source import Observed, build_signals
from isa.controller.policies import Policy
from isa.controller.stabilizer import Stabilizer
from isa.controller.targets import write_targets

log = get_logger(__name__)


class SignalSource(Protocol):
    async def observe(self) -> Observed: ...


class Controller:
    def __init__(
        self,
        cfg: ControllerConfig,
        *,
        policy: Policy,
        stabilizer: Stabilizer,
        backend: ReplicaBackend,
        source: SignalSource,
        names: MetricsMap,
        decision_log: DecisionLog,
        client: httpx.AsyncClient,
        model_name: str,
        close_client: bool = False,
    ) -> None:
        self.cfg = cfg
        self.policy = policy
        self.stabilizer = stabilizer
        self.backend = backend
        self.source = source
        self.names = names
        self.decision_log = decision_log
        self.client = client
        self.model_name = model_name
        self._close_client = close_client

        self.pool = Pool(port_base=cfg.port_base, port_count=cfg.port_count)
        self._lock = asyncio.Lock()
        self._busy: set[str] = set()  # replicas with a warmup or terminate in flight
        self._reserved_ports: set[int] = set()  # held by processes still shutting down
        self._tasks: set[asyncio.Task[None]] = set()
        self._loops: list[asyncio.Task[None]] = []

        # Exposed through the API and metrics.
        self.desired = 0
        self.target = 0
        self.outcomes: Counter[str] = Counter()
        self.last_tick: dict[str, Any] | None = None

    # ---- start / stop ------------------------------------------------------

    async def start(self) -> None:
        self._write_targets()
        self._loops = [
            asyncio.create_task(self._every(self.cfg.tick_s, self.tick), name="tick"),
            asyncio.create_task(
                self._every(self.cfg.health_interval_s, self.supervise), name="supervise"
            ),
        ]
        log.info("controller_started", policy=self.policy.name)

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        # Let in-flight warmups and terminates finish (bounded), so no process is
        # abandoned mid-shutdown; cancel whatever is still running after that.
        if self._tasks:
            _, pending = await asyncio.wait(
                list(self._tasks), timeout=self.cfg.terminate_grace_s + 5
            )
            for t in pending:
                t.cancel()
        # Replicas run in their own process group (D26): nothing else stops them.
        replicas = [r for r in self.pool.active() if r.handle is not None]
        await asyncio.gather(
            *(self.backend.terminate(r.handle, self.cfg.terminate_grace_s) for r in replicas),
            return_exceptions=True,
        )
        write_targets(Path(self.cfg.targets_path), [])
        self.decision_log.close()
        if self._close_client:
            await self.client.aclose()
        log.info("controller_stopped", terminated=len(replicas))

    async def _every(self, interval_s: float, fn: Callable[[], Awaitable[None]]) -> None:
        while True:
            started = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad iteration must not kill the loop: the controller keeps
                # supervising replicas even if, say, one tick hit a bug.
                log.exception("loop_iteration_failed", loop=fn.__name__)
            await asyncio.sleep(max(0.0, interval_s - (time.monotonic() - started)))

    # ---- tick: decide and act ----------------------------------------------

    async def tick(self) -> None:
        # Outside the lock: a slow Prometheus can't block supervise.
        obs = await self.source.observe()
        async with self._lock:
            now = time.time()
            s = build_signals(
                obs,
                ts=now,
                ready=self.pool.count(Phase.READY),
                starting=self.pool.count(Phase.STARTING),
                draining=self.pool.count(Phase.DRAINING),
            )
            decision = self.policy.decide(s)
            action = self.stabilizer.apply(decision.desired, s, now)
            self.desired, self.target = decision.desired, action.target
            self.outcomes[action.reason] += 1

            record = {
                "type": "tick",
                "t": now,
                "policy": self.policy.name,
                "signals": asdict(s),
                "observed": {"stale_reason": obs.stale_reason, "age_s": obs.age_s},
                "decision": asdict(decision),
                "action": {**asdict(action), "delta": action.delta},
            }
            self.decision_log.write(record)
            self.last_tick = record

            if action.delta:
                log.info(
                    "scale_action",
                    reason=action.reason,
                    current=action.current,
                    target=action.target,
                    desired=decision.desired,
                    policy_reason=decision.reason,
                )
            for _ in range(max(action.delta, 0)):
                await self._spawn(now)
            for _ in range(max(-action.delta, 0)):
                await self._begin_drain()

    async def _spawn(self, now: float) -> None:
        replica = self.pool.create(now, exclude_ports=frozenset(self._reserved_ports))
        self._event(replica.replica_id, None, Phase.STARTING, "spawned", port=replica.port)
        try:
            replica.handle = await self.backend.spawn(replica.replica_id, replica.port)
        except Exception as e:
            log.exception("spawn_failed", replica_id=replica.replica_id)
            self._transition(replica, Phase.FAILED, f"spawn_failed: {e!r}"[:200])

    async def _begin_drain(self) -> None:
        ready = self.pool.in_phase(Phase.READY)
        if not ready:
            return
        loads = await asyncio.gather(*(replica_load(self.client, r.url, self.names) for r in ready))
        # Fewest in flight drains fastest. An unreadable load sorts last.
        victim, _ = min(zip(ready, loads, strict=True), key=lambda p: (p[1] is None, p[1] or 0.0))
        self._transition(victim, Phase.DRAINING, "scale_down")

    # ---- supervise: move replicas through their lifecycle -------------------

    async def supervise(self) -> None:
        async with self._lock:
            now = time.time()
            for r in self.pool.active():
                if r.replica_id in self._busy or r.handle is None:
                    continue
                if not self.backend.is_alive(r.handle):
                    self._transition(r, Phase.FAILED, "process_exited")
                elif r.phase is Phase.STARTING:
                    if now - r.created_at > self.cfg.startup_timeout_s:
                        self._fail_and_kill(r, "startup_timeout")
                    elif await probe(self.client, r.url):
                        self._background(self._warm(r), r.replica_id)
                elif r.phase is Phase.READY:
                    if await probe(self.client, r.url):
                        r.health_failures = 0
                    else:
                        r.health_failures += 1
                        if r.health_failures >= self.cfg.health_fail_threshold:
                            self._fail_and_kill(r, f"health_failed_{r.health_failures}x")
                elif r.phase is Phase.DRAINING:
                    draining_for = now - r.changed_at
                    if draining_for < self.cfg.drain_router_grace_s:
                        continue
                    load = await replica_load(self.client, r.url, self.names)
                    if load == 0.0:
                        self._background(self._finish_drain(r, "drained"), r.replica_id)
                    elif draining_for > self.cfg.drain_timeout_s:
                        self._background(self._finish_drain(r, "drain_timeout"), r.replica_id)

    async def _warm(self, r: ManagedReplica) -> None:
        ok = await warmup(self.client, r.url, self.model_name)
        async with self._lock:
            if r.phase is not Phase.STARTING:
                return  # failed while warming
            if ok:
                ready_after_s = round(time.time() - r.created_at, 2)
                self._transition(r, Phase.READY, "warmup_ok", ready_after_s=ready_after_s)
            else:
                # Stays STARTING; the next probe retries, bounded by startup_timeout.
                log.warning("warmup_failed", replica_id=r.replica_id)

    async def _finish_drain(self, r: ManagedReplica, reason: str) -> None:
        await self.backend.terminate(r.handle, self.cfg.terminate_grace_s)
        async with self._lock:
            if r.phase is Phase.DRAINING:
                self._transition(r, Phase.TERMINATED, reason)

    def _fail_and_kill(self, r: ManagedReplica, reason: str) -> None:
        """Out of the pool now, so a replacement can be scheduled; port reserved
        until the process has actually exited."""
        port, handle = r.port, r.handle
        self._reserved_ports.add(port)
        self._transition(r, Phase.FAILED, reason)

        async def kill() -> None:
            try:
                await self.backend.terminate(handle, self.cfg.terminate_grace_s)
            finally:
                self._reserved_ports.discard(port)

        self._background(kill())

    # ---- helpers ------------------------------------------------------------

    def _background(self, coro: Coroutine[Any, Any, None], replica_id: str | None = None) -> None:
        if replica_id is not None:
            self._busy.add(replica_id)
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task[None]) -> None:
            self._tasks.discard(t)
            if replica_id is not None:
                self._busy.discard(replica_id)
                if not t.cancelled() and t.exception() is not None:
                    log.error(
                        "background_task_failed", replica_id=replica_id, error=repr(t.exception())
                    )

        task.add_done_callback(done)

    def _transition(self, r: ManagedReplica, to: Phase, reason: str, **extra: Any) -> None:
        frm = r.phase
        self.pool.transition(r.replica_id, to, time.time())
        self._event(r.replica_id, frm, to, reason, **extra)
        self._write_targets()

    def _event(
        self, replica_id: str, frm: Phase | None, to: Phase, reason: str, **extra: Any
    ) -> None:
        log.info(
            "replica_transition", replica_id=replica_id, frm=frm, to=to, reason=reason, **extra
        )
        self.decision_log.write(
            {
                "type": "lifecycle",
                "t": time.time(),
                "replica_id": replica_id,
                "from": frm,
                "to": to,
                "reason": reason,
                **extra,
            }
        )

    def _write_targets(self) -> None:
        write_targets(Path(self.cfg.targets_path), self.pool.scrape_targets())