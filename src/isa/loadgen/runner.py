"""Open-loop runner: fires every arrival at its scheduled time, whatever the server is doing."""

import asyncio
from dataclasses import dataclass

import httpx

from isa.common.log import get_logger
from isa.loadgen.client import RequestRecord, RunClock, send_one
from isa.loadgen.profile import Profile
from isa.loadgen.report import CsvRecorder
from isa.loadgen.schedule import Arrival, build_prompt

log = get_logger(__name__)

PROGRESS_EVERY_S = 10.0
PREFLIGHT_TIMEOUT_S = 30.0


class PreflightError(RuntimeError):
    """The target couldn't serve a single request: there's nothing to measure."""


async def preflight(client: httpx.AsyncClient, base_url: str, model: str) -> None:
    """One tiny request before the run, so a dead target fails in seconds, not minutes."""
    try:
        async with asyncio.timeout(PREFLIGHT_TIMEOUT_S):
            resp = await client.post(
                f"{base_url}/v1/completions",
                json={"model": model, "prompt": "the", "max_tokens": 1},
            )
    except (httpx.HTTPError, TimeoutError) as e:
        raise PreflightError(f"preflight request to {base_url} failed: {e!r}") from e
    if resp.status_code != 200:
        raise PreflightError(f"preflight got HTTP {resp.status_code}: {resp.text[:200]}")


@dataclass(frozen=True)
class RunStats:
    peak_inflight: int
    elapsed_s: float


async def run(
    profile: Profile,
    schedule: list[Arrival],
    *,
    base_url: str,
    model: str,
    recorder: CsvRecorder,
    max_connections: int = 2000,
    drain_timeout_s: float = 120.0,
    client: httpx.AsyncClient | None = None,
    check_target: bool = True,
) -> RunStats:
    # Build every prompt up front so the dispatch loop does nothing but send.
    prompts = [build_prompt(profile.seed, a.idx, a.prompt_tokens) for a in schedule]
    log.info("prompts_built", count=len(prompts))

    owned = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=None),
            limits=httpx.Limits(
                max_connections=max_connections, max_keepalive_connections=max_connections
            ),
        )

    if check_target:
        try:
            await preflight(client, base_url, model)
        except PreflightError:
            if owned:
                await client.aclose()
            raise
        log.info("preflight_ok", target=base_url)

    # Created after preflight, so the preflight's duration can't show up as
    # dispatch lag on the first arrivals.
    clock = RunClock()
    state = {"inflight": 0, "peak": 0, "done": 0}
    tasks: set[asyncio.Task[None]] = set()

    async def fire(arrival: Arrival, prompt: str) -> None:
        record = RequestRecord(
            req_id=arrival.req_id,
            phase=arrival.phase,
            intended_ts=clock.wall0 + arrival.offset_s,
            prompt_tokens=arrival.prompt_tokens,
            max_tokens=arrival.max_tokens,
        )
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        try:
            await send_one(client, base_url, model, arrival, prompt, clock, record)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # e.g. a malformed SSE line: record it, don't lose the row
            record.error = f"client_error: {e!r}"[:200]
        finally:
            state["inflight"] -= 1
            state["done"] += 1
            recorder.write(record)

    try:
        log.info(
            "run_started",
            profile=profile.name,
            requests=len(schedule),
            duration_s=profile.duration_s,
            target=base_url,
        )
        phase = -1
        next_progress = clock.mono() + PROGRESS_EVERY_S
        for arrival, prompt in zip(schedule, prompts, strict=True):
            if arrival.phase != phase:
                phase = arrival.phase
                log.info("phase_started", phase=phase, rate_rps=profile.phases[phase].rate_rps)
            delay = clock.target(arrival.offset_s) - clock.mono()
            if delay > 0:
                await asyncio.sleep(delay)
            task = asyncio.create_task(fire(arrival, prompt))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            if clock.mono() >= next_progress:
                log.info(
                    "progress", sent=arrival.idx + 1, inflight=state["inflight"], done=state["done"]
                )
                # Reset from now, not from the last deadline: after an idle phase the old
                # deadline is several intervals behind and would fire on every arrival.
                next_progress = clock.mono() + PROGRESS_EVERY_S

        # Honor the profile's full duration, including any trailing idle phase.
        remaining = clock.target(profile.duration_s) - clock.mono()
        if remaining > 0:
            await asyncio.sleep(remaining)

        log.info("arrivals_done", inflight=len(tasks))
        if tasks:
            _, pending = await asyncio.wait(list(tasks), timeout=drain_timeout_s)
            if pending:
                log.warning("drain_timeout", pending=len(pending))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if owned:
            await client.aclose()

    return RunStats(peak_inflight=state["peak"], elapsed_s=clock.mono() - clock.mono0)