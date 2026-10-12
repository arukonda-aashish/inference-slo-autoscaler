from pydantic import Field

from isa.common.config import StrictModel
from isa.controller.backends.mock import MockBackendConfig


class ControllerConfig(StrictModel):
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8100, gt=0, lt=65536)
    prometheus_url: str = "http://127.0.0.1:9090"
    max_signal_age_s: float = Field(default=10.0, gt=0)
    tick_s: float = Field(default=5.0, gt=0)
    health_interval_s: float = Field(default=1.0, gt=0)
    health_fail_threshold: int = Field(default=3, ge=1)
    startup_timeout_s: float = Field(default=120.0, gt=0)
    # The router polls every 1s; wait this long after DRAINING before trusting
    # "zero in flight", so a request routed just before the router noticed lands first.
    drain_router_grace_s: float = Field(default=2.0, ge=0)
    drain_timeout_s: float = Field(default=120.0, gt=0)
    terminate_grace_s: float = Field(default=10.0, gt=0)
    port_base: int = Field(default=8001, gt=0, lt=65536)
    port_count: int = Field(default=16, ge=1)
    targets_path: str = "deploy/prometheus/targets/replicas.json"
    metrics_map: str = "configs/metrics_map.yaml"
    stabilizer: str = "configs/stabilizer.yaml"
    backend: MockBackendConfig = Field(default_factory=MockBackendConfig)