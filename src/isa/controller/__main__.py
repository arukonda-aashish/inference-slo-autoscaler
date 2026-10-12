"""Run the controller: python -m isa.controller --policy configs/policies/queue.yaml"""

import argparse
import time
from pathlib import Path

import httpx
import uvicorn

from isa.common.config import MetricsMap, load_yaml
from isa.common.log import get_logger, setup_logging
from isa.controller.api import create_app
from isa.controller.backends.mock import MockBackend
from isa.controller.config import ControllerConfig
from isa.controller.decision_log import DecisionLog
from isa.controller.metrics_source import MetricsSource
from isa.controller.policies import PolicyFile, make_policy
from isa.controller.runtime import Controller
from isa.controller.stabilizer import Stabilizer, StabilizerConfig


def main() -> None:
    p = argparse.ArgumentParser(description="Inference SLO autoscaler controller")
    p.add_argument("--config", default="configs/controller.yaml")
    p.add_argument("--policy", required=True)
    p.add_argument("--decision-log", help="JSONL path (default: results/dev/decisions-...)")
    args = p.parse_args()

    setup_logging(component="controller")
    log = get_logger("isa.controller")

    cfg = load_yaml(args.config, ControllerConfig)
    policy = make_policy(load_yaml(args.policy, PolicyFile).policy)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = (
        Path(args.decision_log)
        if args.decision_log
        else Path("results/dev") / f"decisions-{policy.name}-{stamp}.jsonl"
    )

    client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    controller = Controller(
        cfg,
        policy=policy,
        stabilizer=Stabilizer(load_yaml(cfg.stabilizer, StabilizerConfig)),
        backend=MockBackend(cfg.backend),
        source=MetricsSource(cfg.prometheus_url, client, cfg.max_signal_age_s),
        names=load_yaml(cfg.metrics_map, MetricsMap),
        decision_log=DecisionLog(log_path),
        client=client,
        model_name=cfg.backend.model_name,
        close_client=True,
    )
    log.info("decision_log", path=str(log_path))
    uvicorn.run(
        create_app(controller),
        host=cfg.api_host,
        port=cfg.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()