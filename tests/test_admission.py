import asyncio
from contextlib import suppress

import httpx
import pytest

from isa.router.admission import Admission, Rejected
from isa.router.app import create_app
from isa.router.balancer import LeastOutstanding
from isa.router.config import AdmissionConfig, RouterConfig
from isa.router.registry import Registry, ReplicaSpec


def registry_with(*ids: str) -> Registry:
    reg = Registry()
    reg.update([ReplicaSpec(replica_id=i, url=f"http://{i}") for i in ids])
    return reg


def make(reg: Registry, **kw) -> Admission:
    opts = {"max_inflight": 1, "max_queue": 10, "max_wait_s": None, **kw}
    a = Admission(reg, LeastOutstanding(), **opts)
    reg.listeners.append(a.pump)
    return a


async def test_fifo_queue_hands_released_slots_to_the_next_waiter():
    reg = registry_with("r1")
    a = make(reg)
    first = await a.acquire()
    second = asyncio.create_task(a.acquire())
    third = asyncio.create_task(a.acquire())
    await asyncio.sleep(0)
    assert a.depth == 2
    a.release(first)
    assert (await second).replica_id == "r1"
    assert not third.done()
    a.release(reg.get("r1"))
    assert (await third).replica_id == "r1"


async def test_replica_that_becomes_ready_takes_the_backlog():
    # The point of late binding: new capacity serves requests already waiting.
    reg = registry_with("r1")
    a = make(reg)
    await a.acquire()  # r1 full
    waiting = asyncio.create_task(a.acquire())
    await asyncio.sleep(0)
    reg.update([
        ReplicaSpec(replica_id="r1", url="http://r1"),
        ReplicaSpec(replica_id="r2", url="http://r2"),
    ])
    assert (await waiting).replica_id == "r2"
    assert a.depth == 0


async def test_full_queue_rejects():
    reg = registry_with("r1")
    a = make(reg, max_queue=0)
    await a.acquire()
    with pytest.raises(Rejected, match="queue_full"):
        await a.acquire()


async def test_queue_timeout_rejects_and_leaks_no_slot():
    reg = registry_with("r1")
    a = make(reg, max_wait_s=0.05)
    slot = await a.acquire()
    with pytest.raises(Rejected, match="queue_timeout"):
        await a.acquire()
    assert a.depth == 0
    a.release(slot)
    assert reg.get("r1").inflight == 0


async def test_cancelled_waiter_never_receives_a_slot():
    reg = registry_with("r1")
    a = make(reg)
    slot = await a.acquire()
    t = asyncio.create_task(a.acquire())
    await asyncio.sleep(0)
    t.cancel()
    with suppress(asyncio.CancelledError):
        await t
    assert a.depth == 0
    a.release(slot)
    assert reg.get("r1").inflight == 0  # not handed to the dead waiter


async def test_disabled_configuration_never_queues():
    a = make(registry_with(), max_inflight=None, max_queue=0)
    with pytest.raises(Rejected):
        await a.acquire()


class Slow(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"ok": True})


async def test_router_sheds_with_429_when_the_queue_is_full():
    upstream = httpx.AsyncClient(mounts={"http://s1": Slow()})
    cfg = RouterConfig(
        replicas=[ReplicaSpec(replica_id="s1", url="http://s1")],
        admission=AdmissionConfig(enabled=True, max_inflight_per_replica=1, max_queue=0),
    )
    app = create_app(cfg, http_client=upstream)
    body = {"model": "m", "prompt": "a", "max_tokens": 1}
    async with upstream, app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as c:
            r1, r2 = await asyncio.gather(
                c.post("/v1/completions", json=body), c.post("/v1/completions", json=body)
            )
            text = (await c.get("/metrics")).text
    assert sorted([r1.status_code, r2.status_code]) == [200, 429]
    assert 'router_rejected_total{reason="queue_full"} 1.0' in text
    assert "router_queue_depth 0.0" in text
    assert app.state.registry.get("s1").inflight == 0