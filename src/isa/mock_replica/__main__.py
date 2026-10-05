"""Run one mock replica: python -m isa.mock_replica --replica-id r1 --port 8001"""

import argparse
import time

import uvicorn

from isa.common.config import MetricsMap, load_yaml
from isa.common.log import get_logger, setup_logging
from isa.mock_replica.engine import EngineConfig
from isa.mock_replica.server import ServerConfig, create_app


def main() -> None:
    p = argparse.ArgumentParser(description="Mock vLLM replica")
    p.add_argument("--replica-id", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--cold-start-s", type=float, default=0.0)
    p.add_argument("--model-name", default="Qwen/Qwen2.5-0.5B-Instruct")
    p.add_argument("--engine-config", default="configs/mock_engine.yaml")
    p.add_argument("--metrics-map", default="configs/metrics_map.yaml")
    args = p.parse_args()

    setup_logging(component=f"mock-{args.replica_id}")
    log = get_logger("isa.mock_replica")

    server = ServerConfig(
        replica_id=args.replica_id,
        model_name=args.model_name,
        host=args.host,
        port=args.port,
        cold_start_s=args.cold_start_s,
    )
    app = create_app(
        server,
        load_yaml(args.engine_config, EngineConfig),
        load_yaml(args.metrics_map, MetricsMap),
    )

    if server.cold_start_s > 0:
        # Not listening yet, so connections are refused — exactly what a health
        # check sees while real vLLM is loading weights.
        log.info("cold_start_begin", seconds=server.cold_start_s)
        time.sleep(server.cold_start_s)

    # log_config=None: uvicorn inherits our logging setup instead of its own.
    uvicorn.run(app, host=server.host, port=server.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()