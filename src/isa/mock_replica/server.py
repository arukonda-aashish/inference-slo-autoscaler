"""Mock replica HTTP server: an OpenAI-compatible /v1/completions over the Engine.

Endpoints mirror the vLLM OpenAI server: POST /v1/completions (SSE when
stream=true), GET /health, GET /metrics.

Prompt length is counted as whitespace-separated words. The load generator
builds prompts from words that are each a single token in the Qwen tokenizer,
so the same prompt has the same length here and on real vLLM (verified against
vLLM's usage.prompt_tokens in Phase 5).
"""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, Field

from isa.common.config import MetricsMap, StrictModel
from isa.common.log import get_logger
from isa.mock_replica.driver import EngineDriver
from isa.mock_replica.engine import Engine, EngineConfig, FinishedRecord, TokenEvent
from isa.mock_replica.metrics import MockMetrics

log = get_logger(__name__)

TOKEN_TEXT = " tok"


class ServerConfig(StrictModel):
    replica_id: str
    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"
    host: str = "127.0.0.1"
    port: int = Field(gt=0, lt=65536)
    cold_start_s: float = Field(default=0.0, ge=0)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    include_usage: bool = False


class CompletionRequest(BaseModel):
    # OpenAI clients send many optional fields (temperature, top_p, ...).
    # Unlike our config models, this one ignores what it doesn't model.
    model_config = ConfigDict(extra="ignore")
    model: str
    prompt: str
    max_tokens: int = Field(default=16, ge=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    ignore_eos: bool = False  # accepted for vLLM parity; the mock never stops early


def _error(status: int, message: str, err_type: str = "BadRequestError") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"object": "error", "message": message, "type": err_type, "code": status},
    )


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def create_app(server: ServerConfig, engine_cfg: EngineConfig, names: MetricsMap) -> FastAPI:
    engine = Engine(engine_cfg)
    metrics = MockMetrics(names, server.model_name, engine)
    driver = EngineDriver(engine, on_step=metrics.on_step)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        driver.start()
        log.info("replica_ready", replica_id=server.replica_id, port=server.port)
        try:
            yield
        finally:
            await driver.stop()
            log.info("replica_stopped", replica_id=server.replica_id)

    app = FastAPI(lifespan=lifespan)
    app.state.driver = driver

    @app.get("/health")
    async def health() -> Response:
        return Response(status_code=200 if driver.healthy else 503)

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/completions")
    async def completions(req: CompletionRequest) -> Response:
        if req.model != server.model_name:
            return _error(404, f"The model `{req.model}` does not exist.", "NotFoundError")

        req_id = f"cmpl-{uuid.uuid4().hex}"
        prompt_tokens = len(req.prompt.split())
        try:
            queue = driver.submit(req_id, prompt_tokens, req.max_tokens)
        except ValueError as e:
            return _error(400, str(e))

        created = int(time.time())
        base = {"id": req_id, "object": "text_completion", "created": created, "model": req.model}

        def usage(rec: FinishedRecord) -> dict:
            return {
                "prompt_tokens": rec.prompt_tokens,
                "completion_tokens": rec.output_tokens,
                "total_tokens": rec.prompt_tokens + rec.output_tokens,
            }

        if not req.stream:
            try:
                while not isinstance(item := await queue.get(), FinishedRecord):
                    pass
            except asyncio.CancelledError:
                driver.abort(req_id)
                raise
            choice = {
                "index": 0,
                "text": TOKEN_TEXT * item.output_tokens,
                "logprobs": None,
                "finish_reason": "length",
            }
            return JSONResponse({**base, "choices": [choice], "usage": usage(item)})

        include_usage = bool(req.stream_options and req.stream_options.include_usage)

        async def stream():
            finished = False
            try:
                while True:
                    item = await queue.get()
                    if isinstance(item, TokenEvent):
                        # The mock always emits exactly max_tokens, so the last
                        # token is known by index and finish_reason rides on it.
                        last = item.index == req.max_tokens
                        choice = {
                            "index": 0,
                            "text": TOKEN_TEXT,
                            "logprobs": None,
                            "finish_reason": "length" if last else None,
                        }
                        yield _sse({**base, "choices": [choice]})
                    else:
                        finished = True
                        if include_usage:
                            yield _sse({**base, "choices": [], "usage": usage(item)})
                        yield b"data: [DONE]\n\n"
                        return
            finally:
                # Runs when Starlette cancels the generator on client disconnect.
                # Freeing the request here is what returns its KV blocks.
                if not finished:
                    driver.abort(req_id)
                    log.debug("client_disconnected", req_id=req_id)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app