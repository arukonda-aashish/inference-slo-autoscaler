"""One streamed request, timed from the client's side."""

import asyncio
import json
import time
from dataclasses import dataclass, fields

import httpx

from isa.loadgen.schedule import Arrival


class RunClock:
    """Monotonic timing with epoch output.

    Durations come from perf_counter, so they can't jump if the system clock is
    adjusted mid-run. Every timestamp is still emitted as epoch seconds, so it
    joins directly against log lines and Prometheus samples.
    """

    def __init__(self) -> None:
        self.mono0 = time.perf_counter()
        self.wall0 = time.time()

    def mono(self) -> float:
        return time.perf_counter()

    def epoch(self) -> float:
        return self.wall0 + (time.perf_counter() - self.mono0)

    def target(self, offset_s: float) -> float:
        """The perf_counter value at which an arrival at offset_s is due."""
        return self.mono0 + offset_s


@dataclass
class RequestRecord:
    req_id: str
    phase: int
    intended_ts: float
    prompt_tokens: int
    max_tokens: int
    sent_ts: float | None = None
    first_token_ts: float | None = None
    last_token_ts: float | None = None
    output_tokens: int = 0
    server_prompt_tokens: int | None = None
    status: int = 0  # HTTP status; 0 = no response (connect error); -1 = cancelled at run end
    replica_id: str = ""
    error: str = ""

    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]


async def send_one(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    arrival: Arrival,
    prompt: str,
    clock: RunClock,
    record: RequestRecord,
) -> None:
    """Fill `record` in place, so partial results survive cancellation."""
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": arrival.max_tokens,
        "stream": True,
        "ignore_eos": True,  # output length set by the experiment, not by the model
        "stream_options": {"include_usage": True},
    }
    record.sent_ts = clock.epoch()
    try:
        async with client.stream("POST", f"{base_url}/v1/completions", json=body) as resp:
            record.status = resp.status_code
            record.replica_id = resp.headers.get("x-replica-id", "")
            if resp.status_code != 200:
                record.error = (await resp.aread()).decode(errors="replace")[:200]
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line.removeprefix("data: ")
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                choices = chunk.get("choices") or []
                if choices and choices[0].get("text"):
                    now = clock.epoch()
                    if record.first_token_ts is None:
                        record.first_token_ts = now
                    record.last_token_ts = now
                    record.output_tokens += 1
                usage = chunk.get("usage")
                if usage:
                    # The server's count is authoritative. Chunk counting is the
                    # fallback: a real server may pack several tokens into one chunk.
                    record.server_prompt_tokens = usage.get("prompt_tokens")
                    record.output_tokens = usage.get("completion_tokens", record.output_tokens)
    except httpx.HTTPError as e:
        record.error = repr(e)[:200]
    except asyncio.CancelledError:
        record.status = -1
        record.error = "cancelled_at_run_end"
        raise