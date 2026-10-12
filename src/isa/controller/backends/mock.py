"""Spawns mock replicas as subprocesses: the local stand-in for vllm serve."""

import asyncio
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import Field

from isa.common.config import StrictModel


class MockBackendConfig(StrictModel):
    kind: Literal["mock"] = "mock"
    cold_start_s: float = Field(default=30.0, ge=0)
    engine_config: str = "configs/mock_engine.yaml"
    metrics_map: str = "configs/metrics_map.yaml"
    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"
    log_dir: str = "logs"


@dataclass
class MockHandle:
    replica_id: str
    process: asyncio.subprocess.Process
    log_path: Path


def _open_log(path: Path) -> BinaryIO:
    # Sync helper: opening a file is blocking I/O, kept out of the async function.
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("ab")


class MockBackend:
    def __init__(self, cfg: MockBackendConfig) -> None:
        self.cfg = cfg

    async def spawn(self, replica_id: str, port: int) -> MockHandle:
        log_path = Path(self.cfg.log_dir) / f"{replica_id}.log"
        log_file = _open_log(log_path)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "isa.mock_replica",
                "--replica-id", replica_id,
                "--port", str(port),
                "--cold-start-s", str(self.cfg.cold_start_s),
                "--engine-config", self.cfg.engine_config,
                "--metrics-map", self.cfg.metrics_map,
                "--model-name", self.cfg.model_name,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                # Own process group: a Ctrl-C to the controller must not reach the
                # replicas directly. The controller owns their shutdown (drain first).
                start_new_session=True,
            )
        finally:
            log_file.close()  # the child holds its own copy of the descriptor
        return MockHandle(replica_id, process, log_path)

    async def terminate(self, handle: MockHandle, grace_s: float) -> None:
        """SIGTERM, wait up to grace_s, then SIGKILL. Callers drain first."""
        process = handle.process
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):  # exited between the check and the signal
            process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_s)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    def is_alive(self, handle: MockHandle) -> bool:
        return handle.process.returncode is None