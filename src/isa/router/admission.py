"""Late binding and admission control.

Without it, the router commits each request to a replica on arrival. A backlog
built during a cold start then stays pinned to whichever replica received it,
and capacity that arrives later only helps new arrivals (measured in 10b).

With it, the router holds requests in one FIFO queue and dispatches each only
when some READY replica has a free slot. A replica that becomes ready later
starts taking the backlog immediately. Admission control falls out of the same
queue: full -> 429 queue_full; waited max_wait_s -> 429 queue_timeout.

Disabled (max_inflight=None, max_queue=0) this is exactly route-on-arrival:
no cap, so a pick always succeeds if any replica is READY, and nothing queues.
"""

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass

from isa.router.balancer import Balancer
from isa.router.registry import Registry, Replica


class Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(eq=False)
class _Waiter:
    exclude: frozenset[str]
    future: asyncio.Future[Replica]


class Admission:
    def __init__(
        self,
        registry: Registry,
        balancer: Balancer,
        *,
        max_inflight: int | None,
        max_queue: int,
        max_wait_s: float | None,
    ) -> None:
        self._registry = registry
        self._balancer = balancer
        self._max_inflight = max_inflight
        self._max_queue = max_queue
        self._max_wait_s = max_wait_s
        self._queue: deque[_Waiter] = deque()

    @property
    def depth(self) -> int:
        return len(self._queue)

    def _pick(self, exclude: frozenset[str]) -> Replica | None:
        candidates = [
            r
            for r in self._registry.ready()
            if r.replica_id not in exclude
            and (self._max_inflight is None or r.inflight < self._max_inflight)
        ]
        return self._balancer.pick(candidates)

    async def acquire(self, exclude: frozenset[str] | None = None, queue: bool = True) -> Replica:
        """A replica with its slot already taken (inflight incremented). Raises Rejected.

        queue=False: take a replica now or give up; used for retries after a
        connect failure, which must never wait behind the whole queue.
        """
        exclude = exclude or frozenset()
        if not self._queue:  # never jump the queue
            replica = self._pick(exclude)
            if replica is not None:
                replica.inflight += 1
                return replica
        if not queue:
            raise Rejected("no_replica")
        if len(self._queue) >= self._max_queue:
            raise Rejected("queue_full")

        waiter = _Waiter(exclude, asyncio.get_running_loop().create_future())
        self._queue.append(waiter)
        try:
            async with asyncio.timeout(self._max_wait_s):  # None: wait indefinitely
                return await waiter.future
        except TimeoutError:
            self._abandon(waiter)
            raise Rejected("queue_timeout") from None
        except asyncio.CancelledError:
            self._abandon(waiter)  # client disconnected while queued
            raise

    def release(self, replica: Replica) -> None:
        """Give a slot back, and hand it straight to the next waiter if any."""
        replica.inflight -= 1
        self.pump()

    def pump(self) -> None:
        """Hand free slots to waiters, strictly first-come-first-served."""
        while self._queue:
            head = self._queue[0]
            if head.future.done():  # cancelled while queued
                self._queue.popleft()
                continue
            replica = self._pick(head.exclude)
            if replica is None:
                return
            self._queue.popleft()
            replica.inflight += 1
            head.future.set_result(replica)

    def _abandon(self, waiter: _Waiter) -> None:
        if waiter.future.done() and not waiter.future.cancelled():
            # A slot was assigned in the same instant we gave up: give it back.
            self.release(waiter.future.result())
            return
        waiter.future.cancel()
        with suppress(ValueError):
            self._queue.remove(waiter)