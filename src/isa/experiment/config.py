from pydantic import Field

from isa.common.config import StrictModel


class ExperimentConfig(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")  # used in the results directory name
    description: str = ""
    policy: str
    profile: str
    controller: str = "configs/controller.yaml"
    router: str = "configs/router_managed.yaml"
    slo: str = "configs/slo.yaml"
    prometheus_url: str = "http://127.0.0.1:9090"
    ready_timeout_s: float = Field(default=180, gt=0)
    settle_s: float = Field(default=15, ge=0)  # clean baseline in Prometheus before load
    cooldown_s: float = Field(default=150, ge=0)  # long enough to capture scale-down
    export_step_s: float = Field(default=2.0, gt=0)  # matches the scrape interval