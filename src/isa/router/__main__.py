"""Run the router: python -m isa.router --config configs/router.yaml"""

import argparse

import uvicorn

from isa.common.config import load_yaml
from isa.common.log import setup_logging
from isa.router.app import create_app
from isa.router.config import RouterConfig


def main() -> None:
    p = argparse.ArgumentParser(description="Inference router")
    p.add_argument("--config", default="configs/router.yaml")
    args = p.parse_args()

    setup_logging(component="router")
    cfg = load_yaml(args.config, RouterConfig)
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()