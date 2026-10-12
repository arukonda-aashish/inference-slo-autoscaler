"""Keeps the router's registry in sync with the controller, which owns replica lifecycle.

Fail static: if the controller is unreachable, keep routing to the last known
set. Dropping every replica because the control plane blinked would turn a
controller restart into a full outage of the data plane.
"""

import asyncio
import time

import httpx

from isa.common.log import get_logger
from isa.router.registry import Registry, ReplicaSpec

log = get_logger(__name__)


async def poll_registry(
    client: httpx.AsyncClient, controller_url: str, registry: Registry, interval_s: float
) -> None:

    log.info("registry_poll_started", controller=controller_url, interval_s=interval_s)
    failing = False
    while True:
        try:
            t0 = time.perf_counter()
            r = await client.get(f"{controller_url}/replicas", timeout=2.0)
            r.raise_for_status()
            specs = [ReplicaSpec.model_validate(x) for x in r.json()]
            registry.update(specs)
            took_ms = round((time.perf_counter() - t0) * 1000, 1)
            log.debug("registry_polled", replicas=len(specs), took_ms=took_ms)
            if failing:
                log.info("controller_reachable_again")
                failing = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not failing:  # log once per outage, not once per poll
                log.warning("controller_unreachable_keeping_last_known", error=repr(e))
                failing = True
        await asyncio.sleep(interval_s)