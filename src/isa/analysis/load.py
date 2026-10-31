"""Loads one run directory into plain Python structures. Read-only."""

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Request:
    phase: int
    intended_ts: float
    first_token_ts: float | None
    status: int
    error: str

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    @property
    def ttft_s(self) -> float | None:
        """From intended arrival, as the load generator measures it (D13)."""
        if not self.ok or self.first_token_ts is None:
            return None
        return self.first_token_ts - self.intended_ts


@dataclass
class Run:
    path: Path
    name: str
    policy: str
    slo_ttft_s: float
    run: dict
    meta: dict
    requests: list[Request]
    ticks: list[dict]
    events: list[dict]

    @property
    def valid(self) -> bool:
        return bool(self.run.get("valid"))

    @property
    def phases(self) -> list[dict]:
        return self.run["phases"]

    @property
    def load_start(self) -> float:
        return self.run["load_started_at"]

    @property
    def load_end(self) -> float:
        return self.phases[-1]["end"]


def _float(x: str) -> float | None:
    return float(x) if x not in ("", None) else None


def load_run(path: Path) -> Run:
    run = json.loads((path / "run.json").read_text())
    if not run.get("phases"):
        raise ValueError(f"{path}: run.json has no phase windows (did the load phase run?)")
    meta = json.loads((path / "meta.json").read_text())
    cfg = path / "config"
    exp = yaml.safe_load((cfg / "experiment.yaml").read_text())
    slo = yaml.safe_load((cfg / "slo.yaml").read_text())

    with (path / "requests.csv").open() as f:
        requests = [
            Request(
                phase=int(r["phase"]),
                intended_ts=float(r["intended_ts"]),
                first_token_ts=_float(r["first_token_ts"]),
                status=int(r["status"]),
                error=r["error"],
            )
            for r in csv.DictReader(f)
        ]

    ticks: list[dict] = []
    events: list[dict] = []
    for line in (path / "decisions.jsonl").read_text().splitlines():
        record = json.loads(line)
        (ticks if record["type"] == "tick" else events).append(record)

    return Run(
        path=path,
        name=exp["name"],
        policy=Path(exp["policy"]).stem,
        slo_ttft_s=float(slo["ttft_p95_s"]),
        run=run,
        meta=meta,
        requests=requests,
        ticks=ticks,
        events=events,
    )