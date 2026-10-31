from typing import Literal

from pydantic import Field, model_validator

from isa.common.config import StrictModel
from isa.router.registry import ReplicaSpec


class AdmissionConfig(StrictModel):
    """Off by default: the router then routes each request on arrival, as before."""

    enabled: bool = False
    max_inflight_per_replica: int = Field(default=24, ge=1)  # a replica's slots
    max_queue: int = Field(default=10_000, ge=0)  # router queue length before 429
    max_queue_wait_s: float | None = Field(default=None, gt=0)  # waited this long -> 429


class RouterConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)
    balancer: Literal["least_outstanding", "round_robin"] = "least_outstanding"
    # Distinct replicas tried when connecting fails. Only connect failures are
    # retried: nothing reached the replica, so a retry can't duplicate work.
    connect_attempts: int = Field(default=2, ge=1)
    # Exactly one replica source: a static list, or a controller to poll.
    replicas: list[ReplicaSpec] = Field(default_factory=list)
    controller_url: str | None = None
    registry_poll_s: float = Field(default=1.0, gt=0)
    admission: AdmissionConfig = Field(default_factory=AdmissionConfig)

    @model_validator(mode="after")
    def _one_source(self) -> "RouterConfig":
        if self.replicas and self.controller_url:
            raise ValueError("set either replicas (static) or controller_url (managed), not both")
        return self