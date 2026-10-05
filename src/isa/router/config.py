from typing import Literal

from pydantic import Field

from isa.common.config import StrictModel
from isa.router.registry import ReplicaSpec


class RouterConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)
    balancer: Literal["least_outstanding", "round_robin"] = "least_outstanding"
    # Distinct replicas tried when connecting fails. Only connect failures are
    # retried: nothing reached the replica, so a retry can't duplicate work.
    connect_attempts: int = Field(default=2, ge=1)
    replicas: list[ReplicaSpec] = Field(default_factory=list)