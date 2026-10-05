"""Router: the single entry point. Balances across READY replicas and relays responses.

Invariant: every request that increments a replica's in-flight count decrements
it exactly once, however the request ends. Balancing, and later admission
control, are only as accurate as that count.
"""

import json
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import Receive, Scope, Send

from isa.common.log import get_logger
from isa.router.balancer import make_balancer
from isa.router.config import RouterConfig
from isa.router.metrics import RouterMetrics
from isa.router.registry import Registry, Replica

log = get_logger(__name__)

# connect: fail fast, so a dead replica costs at most ~2s before trying another.
# read: None. Under overload a request can legitimately queue 30s+ before its
#   first token. That's the latency we're measuring, not something to cut off.
# pool: waiting this long for a free connection means the router itself is saturated.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=2.0, read=None, write=10.0, pool=5.0)
UPSTREAM_LIMITS = httpx.Limits(max_connections=1000, max_keepalive_connections=200)


class _TrackedStreamingResponse(StreamingResponse):
    """Runs on_close when the response finishes, however it finishes.

    A generator's own `finally` never runs if the client disconnects before
    Starlette starts iterating it. Wrapping __call__ covers that case too.
    """

    def __init__(self, *args, on_close: Callable[[], Awaitable[None]], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._on_close()


def create_app(cfg: RouterConfig, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    """http_client is injectable so tests can route upstream calls to in-process apps."""
    registry = Registry()
    registry.update(cfg.replicas)
    balancer = make_balancer(cfg.balancer)
    metrics = RouterMetrics(registry)
    holder: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        owned = http_client is None
        holder["client"] = http_client or httpx.AsyncClient(
            timeout=UPSTREAM_TIMEOUT, limits=UPSTREAM_LIMITS
        )
        log.info(
            "router_ready", port=cfg.port, balancer=cfg.balancer, replicas=len(cfg.replicas)
        )
        try:
            yield
        finally:
            if owned:
                await holder["client"].aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.registry = registry

    def error(status: int, message: str) -> JSONResponse:
        metrics.requests.labels(code=str(status)).inc()
        return JSONResponse(
            status_code=status,
            content={"object": "error", "message": message, "type": "RouterError", "code": status},
        )

    @app.get("/health")
    async def health() -> Response:
        return Response(status_code=200)

    @app.get("/replicas")
    async def replicas() -> list[dict]:
        return [
            {
                "replica_id": r.replica_id,
                "url": r.url,
                "state": r.state.value,
                "inflight": r.inflight,
            }
            for r in registry.all()
        ]

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/completions")
    async def completions(request: Request) -> Response:
        t0 = time.perf_counter()
        body = await request.body()
        try:
            stream = bool(json.loads(body).get("stream", False))
        except (ValueError, AttributeError):
            return error(400, "request body must be a JSON object")

        client = holder["client"]
        tried: set[str] = set()
        for _ in range(cfg.connect_attempts):
            candidates = [r for r in registry.ready() if r.replica_id not in tried]
            replica = balancer.pick(candidates)
            if replica is None:
                break
            tried.add(replica.replica_id)

            replica.inflight += 1
            upstream_req = client.build_request(
                "POST",
                f"{replica.url}/v1/completions",
                content=body,  # forwarded byte-for-byte; the router never rewrites requests
                headers={"content-type": "application/json"},
            )
            try:
                upstream = await client.send(upstream_req, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Nothing reached the replica, so trying another is safe.
                replica.inflight -= 1
                metrics.upstream_errors.labels(replica=replica.replica_id).inc()
                log.warning("upstream_connect_failed", replica_id=replica.replica_id, error=repr(e))
                continue
            except httpx.PoolTimeout:
                replica.inflight -= 1
                return error(503, "router connection pool exhausted")
            except BaseException:
                # Includes CancelledError if the client disconnects mid-connect.
                replica.inflight -= 1
                raise

            log.debug("request_routed", replica_id=replica.replica_id, stream=stream)
            return await relay(upstream, replica, stream, t0)

        if not tried:
            return error(503, "no ready replicas")
        return error(502, f"could not connect to any replica (tried {len(tried)})")

    async def relay(
        upstream: httpx.Response, replica: Replica, stream: bool, t0: float
    ) -> Response:
        headers = {"x-replica-id": replica.replica_id}
        media_type = upstream.headers.get("content-type")
        status = upstream.status_code

        if not stream or status != 200:
            # Buffered path. Also taken for upstream errors on stream=true, so the
            # client gets a normal JSON error rather than a broken event stream.
            try:
                content = await upstream.aread()
            except httpx.HTTPError as e:
                metrics.upstream_errors.labels(replica=replica.replica_id).inc()
                log.warning("upstream_read_failed", replica_id=replica.replica_id, error=repr(e))
                return error(502, "upstream failed mid-response")
            finally:
                replica.inflight -= 1  # before the await, so cancellation can't skip it
                await upstream.aclose()
            metrics.requests.labels(code=str(status)).inc()
            return Response(content, status_code=status, media_type=media_type, headers=headers)

        closed = False
        completed = False

        async def body():
            nonlocal completed
            first = True
            try:
                async for chunk in upstream.aiter_raw():
                    if first:
                        metrics.ttft.observe(time.perf_counter() - t0)
                        first = False
                    yield chunk
                completed = True
            except httpx.HTTPError as e:
                # Headers are already sent: the client just sees the stream end early.
                metrics.upstream_errors.labels(replica=replica.replica_id).inc()
                log.warning("upstream_stream_broken", replica_id=replica.replica_id, error=repr(e))

        async def on_close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            replica.inflight -= 1  # before the await, so cancellation can't skip it
            if not completed:
                log.debug("stream_closed_early", replica_id=replica.replica_id)
            # Closing the upstream connection is what tells the replica the client
            # is gone, which makes it abort the request and free its KV blocks.
            await upstream.aclose()

        metrics.requests.labels(code="200").inc()
        return _TrackedStreamingResponse(
            body(), status_code=200, media_type=media_type, headers=headers, on_close=on_close
        )

    return app