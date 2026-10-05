"""Run a load profile: python -m isa.loadgen --profile configs/profiles/smoke.yaml

Exit codes: 0 valid run, 2 run completed but failed its self-validity checks,
3 preflight failed (target unreachable or erroring; nothing was measured).
"""

import argparse
import asyncio
import time
from pathlib import Path

from isa.common.config import load_yaml
from isa.common.log import get_logger, setup_logging
from isa.loadgen.profile import Profile
from isa.loadgen.report import CsvRecorder, summarize, write_summary
from isa.loadgen.runner import PreflightError, run
from isa.loadgen.schedule import build_schedule


def main() -> int:
    p = argparse.ArgumentParser(description="Open-loop load generator")
    p.add_argument("--profile", required=True)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    p.add_argument("--out", help="CSV path (default: results/dev/<profile>-<timestamp>.csv)")
    p.add_argument("--max-model-len", type=int, default=4096)
    p.add_argument("--max-connections", type=int, default=2000)
    p.add_argument("--drain-timeout-s", type=float, default=120.0)
    args = p.parse_args()

    setup_logging(component="loadgen")
    log = get_logger("isa.loadgen")

    profile = load_yaml(args.profile, Profile)
    schedule = build_schedule(profile, max_model_len=args.max_model_len)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else Path("results/dev") / f"{profile.name}-{stamp}.csv"

    recorder = CsvRecorder(out)
    try:
        stats = asyncio.run(
            run(
                profile,
                schedule,
                base_url=args.url.rstrip("/"),
                model=args.model,
                recorder=recorder,
                max_connections=args.max_connections,
                drain_timeout_s=args.drain_timeout_s,
            )
        )
    except PreflightError as e:
        recorder.close()
        out.unlink(missing_ok=True)  # don't leave a header-only CSV behind
        log.error("preflight_failed", error=str(e))
        return 3
    finally:
        recorder.close()

    summary = summarize(recorder.records, profile.name, stats.peak_inflight, args.max_connections)
    summary_path = out.with_suffix(".summary.json")
    write_summary(summary_path, summary)

    for phase, d in summary.by_phase.items():
        log.info("phase_summary", phase=phase, **d)
    log.info(
        "run_summary",
        requests=summary.requests,
        by_status=summary.by_status,
        ttft_p50_s=summary.ttft_p50_s,
        ttft_p95_s=summary.ttft_p95_s,
        ttft_p99_s=summary.ttft_p99_s,
        lag_p99_s=summary.lag_p99_s,
        peak_inflight=summary.peak_inflight,
        csv=str(out),
        summary=str(summary_path),
    )
    if not summary.valid:
        for reason in summary.invalid_reasons:
            log.error("run_invalid", reason=reason)
        return 2
    log.info("run_valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())