"""Run one experiment: python -m isa.experiment configs/experiments/e3_queue_burst.yaml

Exit codes: 0 valid run, 1 run completed but invalid, 2 preconditions failed
(nothing was started).
"""

import argparse
from pathlib import Path

from isa.common.log import get_logger, setup_logging
from isa.experiment.runner import PreconditionError, run_experiment


def main() -> int:
    p = argparse.ArgumentParser(description="Run one experiment end to end")
    p.add_argument("experiment", help="path to configs/experiments/<name>.yaml")
    p.add_argument("--results-root", default="results")
    args = p.parse_args()

    setup_logging(component="experiment")
    log = get_logger("isa.experiment")
    try:
        _, valid = run_experiment(Path(args.experiment), Path(args.results_root))
    except PreconditionError as e:
        log.error("precondition_failed", problems=str(e))
        return 2
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())