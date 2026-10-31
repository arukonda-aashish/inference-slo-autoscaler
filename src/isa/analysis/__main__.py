"""Analyze experiment runs.

  python -m isa.analysis report results/<run> [...]   per-run report.md, metrics.json, timeline.png
  python -m isa.analysis compare [results/<run> ...]  comparison table (default: every run)
"""

import argparse
from pathlib import Path

from isa.analysis.report import compare, write_run_report
from isa.common.log import get_logger, setup_logging


def run_dirs(given: list[str]) -> list[Path]:
    if given:
        return [Path(g) for g in given]
    return sorted(p.parent for p in Path("results").glob("*/run.json") if p.parent.name != "dev")


def main() -> int:
    p = argparse.ArgumentParser(description="Analyze experiment runs")
    sub = p.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("runs", nargs="*")
    cmp = sub.add_parser("compare")
    cmp.add_argument("runs", nargs="*")
    cmp.add_argument("--out", default="results/comparison.md")
    args = p.parse_args()

    setup_logging(component="analysis")
    log = get_logger("isa.analysis")
    dirs = run_dirs(args.runs)
    metrics = []
    for d in dirs:
        try:
            metrics.append(write_run_report(d))
            log.info("report_written", run=d.name, path=str(d / "analysis" / "report.md"))
        except (OSError, ValueError, KeyError) as e:
            log.error("report_failed", run=d.name, error=repr(e))

    if args.cmd == "compare":
        table = compare(metrics)
        Path(args.out).write_text(table)
        print(table)
        log.info("comparison_written", path=args.out, runs=len(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())