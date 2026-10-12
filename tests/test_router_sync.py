import asyncio
from contextlib import suppress

import httpx
import pytest
from pydantic import ValidationError

from isa.router.app import create_app
from isa.router.config import RouterConfig
from isa.router.registry import Registry, ReplicaSpec, ReplicaState
from isa.router.sync import poll_registry


async def wait_until(cond, within_s: float = 2.0) -> None:
    try:
        async with asyncio.timeout(within_s):
            # Polling is deliberate: registry state changes without signalling.
            while not cond():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"condition not met within {within_s}s") from None


async def test_poll_tracks_controller_and_fails_static():
    specs = [{"replica_id": "r1", "url": "http://127.0.0.1:8001", "state": "ready"}]
    controller = {"up": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if not controller["up"]:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=specs)

    registry = Registry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(poll_registry(client, "http://controller", registry, 0.01))
        try:
            await wait_until(lambda: registry.get("r1") is not None)

            controller["up"] = False
            await asyncio.sleep(0.05)
            assert [r.replica_id for r in registry.ready()] == ["r1"]  # fail static

            specs[0]["state"] = "draining"
            controller["up"] = True
            await wait_until(lambda: registry.get("r1").state is ReplicaState.DRAINING)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def test_router_config_requires_one_replica_source():
    with pytest.raises(ValidationError, match="not both"):
        RouterConfig(
            replicas=[ReplicaSpec(replica_id="r1", url="http://x")],
            controller_url="http://controller",
        )

async def test_router_app_polls_controller_when_managed():
    # The wiring test: a router configured with controller_url must actually
    # start polling. Found missing in a live run where the router's registry
    # stayed empty while the controller had a ready replica.
    specs = [{"replica_id": "r1", "url": "http://r1", "state": "ready"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/replicas"
        return httpx.Response(200, json=specs)

    client = httpx.AsyncClient(mounts={"http://controller": httpx.MockTransport(handler)})
    app = create_app(
        RouterConfig(controller_url="http://controller", registry_poll_s=0.01), http_client=client
    )
    async with client, app.router.lifespan_context(app):
        await wait_until(lambda: app.state.registry.get("r1") is not None)