"""One experiment run, end to end, into one self-describing results directory.

  results/<stamp>_<name>/
    config/                every config file the run used, copied verbatim
    meta.json              git sha + dirty flag, host, python version
    requests.csv           load generator: one row per request
    requests.summary.json  load generator: summary and validity checks
    decisions.jsonl        controller: every tick and lifecycle transition
    prometheus/*.csv       range exports of signals and controller state
    run.json               outcome: timing, phase windows, exit codes, validity
    logs/*.log             JSON process logs (gitignored)

A run is valid only if the load generator's own checks passed, no replica
outlived the controller, and every Prometheus export succeeded.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from isa.common.config import load_yaml
from isa.common.log import get_logger
from isa.controller.config import ControllerConfig
from isa.experiment.config import ExperimentConfig
from isa.experiment.procs import Child, port_in_use, start_child, stop_child
from isa.experiment.prom_export import EXPORTS, export_range, write_rows
from isa.loadgen.profile import Profile
from isa.router.config import RouterConfig

log = get_logger(__name__)


class PreconditionError(RuntimeError):
    pass


def check_preconditions(
    exp: ExperimentConfig, ctrl: ControllerConfig, router: RouterConfig, client: httpx.Client
) -> None:
    problems: list[str] = []
    try:
        if client.get(f"{exp.prometheus_url}/-/ready").status_code != 200:
            problems.append("Prometheus is not ready")
    except httpx.HTTPError:
        problems.append(
            f"Prometheus unreachable at {exp.prometheus_url} (run scripts/dev_observability.sh)"
        )
    for name, port in (("router", router.port), ("controller", ctrl.api_port)):
        if port_in_use(port):
            problems.append(f"port {port} ({name}) is already in use")
    busy = [p for p in range(ctrl.port_base, ctrl.port_base + ctrl.port_count) if port_in_use(p)]
    if busy:
        problems.append(f"replica ports in use: {busy} (stray replicas? pkill -f isa.mock_replica)")
    if problems:
        raise PreconditionError("; ".join(problems))


def snapshot(dest: Path, files: dict[str, str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, src in files.items():
        shutil.copyfile(src, dest / name)


def git_meta() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            return None
        return out.stdout.strip()

    status = git("status", "--porcelain", "--", ".", ":(exclude)results")
    return {"git_sha": git("rev-parse", "HEAD"), "git_dirty": None if status is None else bool(status)}


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))


def parse_event_time(log_path: Path, event: str) -> float | None:
    """Epoch `t` of the first JSON log line with this event name."""
    for line in log_path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # e.g. a non-JSON line from a library
        if isinstance(record, dict) and record.get("event") == event:
            return record.get("t")
    return None


def phase_windows(profile: Profile, started_at: float) -> list[dict[str, float]]:
    windows, t = [], started_at
    for i, phase in enumerate(profile.phases):
        windows.append(
            {"phase": i, "rate_rps": phase.rate_rps, "start": t, "end": t + phase.duration_s}
        )
        t += phase.duration_s
    return windows


def wait_ready(client: httpx.Client, router_url: str, timeout_s: float, children: list[Child]) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        for c in children:
            if c.proc.poll() is not None:
                raise RuntimeError(f"{c.name} exited with code {c.proc.returncode}; see {c.log_path}")
        try:
            replicas = client.get(f"{router_url}/replicas").json()
            if any(r.get("state") == "ready" for r in replicas):
                return time.monotonic() - t0
        except (httpx.HTTPError, ValueError):
            pass  # router or controller still starting
        time.sleep(0.5)
    raise RuntimeError(f"no ready replica within {timeout_s}s")


def export_all(
    client: httpx.Client, exp: ExperimentConfig, dest: Path, start: float, end: float
) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for name, expr in EXPORTS.items():
        try:
            rows = export_range(client, exp.prometheus_url, expr, start, end, exp.export_step_s)
            write_rows(dest / f"{name}.csv", rows)
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            errors.append(f"{name}: {e!r}"[:200])
    return errors


def run_experiment(exp_path: Path, results_root: Path = Path("results")) -> tuple[Path, bool]:
    exp = load_yaml(exp_path, ExperimentConfig)
    ctrl = load_yaml(exp.controller, ControllerConfig)
    router = load_yaml(exp.router, RouterConfig)
    profile = load_yaml(exp.profile, Profile)
    router_url = f"http://{router.host}:{router.port}"
    replica_ports = range(ctrl.port_base, ctrl.port_base + ctrl.port_count)

    with httpx.Client(timeout=5.0) as client:
        check_preconditions(exp, ctrl, router, client)

        run_dir = results_root / f"{time.strftime('%Y%m%d-%H%M%S')}_{exp.name}"
        run_dir.mkdir(parents=True)
        snapshot(
            run_dir / "config",
            {
                "experiment.yaml": str(exp_path),
                "policy.yaml": exp.policy,
                "profile.yaml": exp.profile,
                "controller.yaml": exp.controller,
                "router.yaml": exp.router,
                "stabilizer.yaml": ctrl.stabilizer,
                "slo.yaml": exp.slo,
                "mock_engine.yaml": ctrl.backend.engine_config,
                "metrics_map.yaml": ctrl.metrics_map,
            },
        )
        meta = {
            "experiment": exp.name,
            "description": exp.description,
            "created_at": time.time(),
            "host": platform.node(),
            "python": platform.python_version(),
            **git_meta(),
        }
        write_json(run_dir / "meta.json", meta)
        if meta["git_dirty"]:
            log.warning("git_tree_dirty", hint="commit before runs you intend to keep")
        log.info("run_dir", path=str(run_dir))

        outcome: dict[str, Any] = {"experiment": exp.name, "started_at": time.time()}
        children: list[Child] = []
        logs = run_dir / "logs"
        try:
            children.append(
                start_child(
                    "controller",
                    "isa.controller",
                    [
                        "--config", exp.controller,
                        "--policy", exp.policy,
                        "--decision-log", str(run_dir / "decisions.jsonl"),
                    ],
                    logs / "controller.log",
                )
            )
            children.append(
                start_child("router", "isa.router", ["--config", exp.router], logs / "router.log")
            )

            outcome["ready_after_s"] = round(
                wait_ready(client, router_url, exp.ready_timeout_s, children), 2
            )
            log.info("pool_ready", after_s=outcome["ready_after_s"], settle_s=exp.settle_s)
            time.sleep(exp.settle_s)

            log.info("load_starting", profile=profile.name, duration_s=profile.duration_s)
            loadgen_log = logs / "loadgen.log"
            with loadgen_log.open("ab") as f:
                result = subprocess.run(
                    [
                        sys.executable, "-m", "isa.loadgen",
                        "--profile", exp.profile,
                        "--url", router_url,
                        "--model", ctrl.backend.model_name,
                        "--out", str(run_dir / "requests.csv"),
                    ],
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "ISA_LOG_FORMAT": "json"},
                    check=False,
                )
            outcome["loadgen_exit"] = result.returncode
            started = parse_event_time(loadgen_log, "run_started")
            outcome["load_started_at"] = started
            outcome["phases"] = phase_windows(profile, started) if started else None
            log.info("load_finished", exit_code=result.returncode)

            log.info("cooldown", seconds=exp.cooldown_s)
            time.sleep(exp.cooldown_s)
            outcome["ended_at"] = time.time()
            outcome["export_errors"] = export_all(
                client, exp, run_dir / "prometheus", outcome["started_at"] - 10, outcome["ended_at"]
            )
        finally:
            # Reverse order: router first, then the controller, which terminates its replicas.
            outcome["exit_codes"] = {c.name: stop_child(c) for c in reversed(children)}
            outcome["orphan_ports"] = [p for p in replica_ports if port_in_use(p)]
            outcome["valid"] = (
                outcome.get("loadgen_exit") == 0
                and not outcome["orphan_ports"]
                and outcome.get("export_errors") == []
            )
            write_json(run_dir / "run.json", outcome)
            log.info("run_finished", valid=outcome["valid"], run_dir=str(run_dir))

    return run_dir, outcome["valid"]