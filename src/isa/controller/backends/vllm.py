"""Spawns real vLLM replicas: `vllm serve` subprocesses sharing one GPU.

Process handling (terminate, is_alive) is identical to the mock's, so this
reuses it; only the command line differs.
"""

import asyncio
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field

from isa.common.config import StrictModel
from isa.controller.backends.mock import MockBackend, MockHandle, _open_log


class VllmBackendConfig(StrictModel):
    kind: Literal["vllm"]
    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"
    gpu_memory_utilization: float = Field(default=0.28, gt=0, le=1)  # 3 replicas fit a 3090
    max_model_len: int = Field(default=4096, gt=0)
    # vLLM flags drift between releases. Anything version-specific goes here, so
    # it can be fixed on the pod by editing YAML, not code.
    extra_args: list[str] = Field(default_factory=lambda: ["--no-enable-prefix-caching"])
    # The vllm entry point next to the interpreter running the controller (the uv venv).
    executable: str = Field(default_factory=lambda: str(Path(sys.executable).with_name("vllm")))
    log_dir: str = "logs"


def build_argv(cfg: VllmBackendConfig, port: int) -> list[str]:
    return [
        cfg.executable, "serve", cfg.model_name,
        "--host", "127.0.0.1",
        "--port", str(port),
        "--gpu-memory-utilization", str(cfg.gpu_memory_utilization),
        "--max-model-len", str(cfg.max_model_len),
        *cfg.extra_args,
    ]


class VllmBackend(MockBackend):
    def __init__(self, cfg: VllmBackendConfig) -> None:  # noqa: D107
        self.cfg = cfg  # terminate/is_alive (inherited) don't read cfg

    async def spawn(self, replica_id: str, port: int) -> MockHandle:
        log_path = Path(self.cfg.log_dir) / f"{replica_id}.log"
        log_file = _open_log(log_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *build_argv(self.cfg, port),
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # the controller owns shutdown (D26)
            )
        finally:
            log_file.close()
        return MockHandle(replica_id, process, log_path)