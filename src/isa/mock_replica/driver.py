"""Async driver: runs the pure Engine in real time and delivers tokens to waiters.

Loop: step the engine at the current loop time, sleep for the step's simulated
duration, then hand each token to its request's queue. When there's no work, it
parks on an Event until submit() wakes it, so an idle replica costs no CPU.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress

from isa.common.log import get_logger
from isa.mock_replica.engine import Engine, FinishedRecord, StepResult, TokenEvent

log = get_logger(__name__)

# A request's queue receives one TokenEvent per token, then exactly one
# FinishedRecord. An aborted request receives nothing further.
Delivery = TokenEvent | FinishedRecord


class EngineDriver:
    def __init__(
        self, engine: Engine, on_step: Callable[[StepResult], None] | None = None
    ) -> None:
        self.engine = engine
        self._on_step = on_step
        self._queues: dict[str, asyncio.Queue[Delivery]] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def healthy(self) -> bool:
        """False if the loop was never started, was stopped, or crashed."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="engine-driver")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def submit(self, req_id: str, prompt_tokens: int, max_tokens: int) -> asyncio.Queue[Delivery]:
        """Raises ValueError (from the engine) if the request is invalid."""
        now = asyncio.get_running_loop().time()
        self.engine.submit(req_id, prompt_tokens, max_tokens, now=now)
        queue: asyncio.Queue[Delivery] = asyncio.Queue()
        self._queues[req_id] = queue
        self._wake.set()
        return queue

    def abort(self, req_id: str) -> None:
        self._queues.pop(req_id, None)
        if self.engine.abort(req_id):
            log.debug("req_aborted", req_id=req_id)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        log.info("driver_started")
        try:
            while True:
                if not self.engine.has_work:
                    # No await between the check and clear(), so a submit() can't
                    # slip in between and get its wake-up lost.
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                result = self.engine.step(loop.time())
                # A zero-duration step (preemption-only) still yields, so the
                # loop never starves the server of the event loop.
                await asyncio.sleep(result.duration_s)
                self._deliver(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("driver_crashed")
            raise

    def _deliver(self, result: StepResult) -> None:
        for ev in result.tokens:
            queue = self._queues.get(ev.req_id)
            if queue is not None:  # None: client aborted during the sleep
                queue.put_nowait(ev)
        for rec in result.finished:
            queue = self._queues.pop(rec.req_id, None)
            if queue is not None:
                queue.put_nowait(rec)
        if self._on_step is not None:
            self._on_step(result)