import json
import socket
from pathlib import Path

import httpx
import pytest
import yaml

from isa.common.config import load_yaml
from isa.experiment.config import ExperimentConfig
from isa.experiment.procs import port_in_use
from isa.experiment.prom_export import EXPORTS, export_range
from isa.experiment.runner import parse_event_time, phase_windows
from isa.loadgen.profile import Profile

ROOT = Path(__file__).resolve().parent.parent


def test_repo_experiments_load_and_reference_existing_files():
    paths = sorted((ROOT / "configs" / "experiments").glob("*.yaml"))
    assert paths
    for path in paths:
        exp = load_yaml(path, ExperimentConfig)
        assert exp.name == path.stem
        for ref in (exp.policy, exp.profile, exp.controller, exp.router, exp.slo):
            assert (ROOT / ref).is_file(), f"{path.name}: {ref} does not exist"


def test_port_in_use():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert port_in_use(port)
    assert not port_in_use(port)


def test_export_range_flattens_matrix():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query_range"
        return httpx.Response(200, json={"status": "success", "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"__name__": "x", "phase": "ready"}, "values": [[1.0, "2"], [3.0, "4"]]},
                {"metric": {"phase": "starting"}, "values": [[1.0, "0"]]},
            ],
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = export_range(client, "http://prom", "controller_replicas", 0, 10, 2)
    assert rows == [
        {"ts": 1.0, "series": "phase=ready", "value": 2.0},
        {"ts": 3.0, "series": "phase=ready", "value": 4.0},
        {"ts": 1.0, "series": "phase=starting", "value": 0.0},
    ]


def test_exported_isa_series_are_recorded_by_rules():
    doc = yaml.safe_load((ROOT / "deploy" / "prometheus" / "rules.yml").read_text())
    recorded = {r["record"] for g in doc["groups"] for r in g["rules"]}
    exported = {e for e in EXPORTS.values() if e.startswith("isa:")}
    assert exported <= recorded, exported - recorded


def test_parse_event_time_skips_non_json(tmp_path):
    log = tmp_path / "loadgen.log"
    log.write_text(
        "not json\n"
        + json.dumps({"event": "prompts_built", "t": 1.0}) + "\n"
        + json.dumps({"event": "run_started", "t": 2.5}) + "\n"
    )
    assert parse_event_time(log, "run_started") == 2.5
    assert parse_event_time(log, "never_logged") is None


def test_phase_windows():
    profile = load_yaml(ROOT / "configs" / "profiles" / "burst_short.yaml", Profile)
    w = phase_windows(profile, started_at=1000.0)
    assert [(x["start"], x["end"]) for x in w] == [(1000, 1060), (1060, 1090), (1090, 1210)]
    assert [x["rate_rps"] for x in w] == [4, 16, 4]
    assert w[1]["rate_rps"] == pytest.approx(16.0)