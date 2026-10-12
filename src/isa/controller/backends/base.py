"""The backend interface: how replicas are actually started and stopped.

The controller never knows whether it's managing mock processes or vLLM. A
ProcessBackend for vllm serve (Phase 5) implements the same three methods.
"""

from typing import Protocol


class ReplicaBackend(Protocol):
    async def spawn(self, replica_id: str, port: int) -> object: ...

    async def terminate(self, handle: object, grace_s: float) -> None: ...

    def is_alive(self, handle: object) -> bool: ...